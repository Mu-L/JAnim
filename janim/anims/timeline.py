from __future__ import annotations

import inspect
import math
import time
import traceback
from abc import ABCMeta, abstractmethod
from bisect import bisect, insort
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterable, Self

import moderngl as mgl
import numpy as np

from janim.anims.anim_stack import AnimStack
from janim.anims.animation import Animation, TimeAligner, TimeRange
from janim.anims.composition import AnimGroup
from janim.anims.updater import updater_params_ctx
from janim.camera.camera import Camera
from janim.constants import DEFAULT_DURATION
from janim.exception import TimelineLookupError
from janim.items.audio import Audio
from janim.items.item import Item
from janim.locale.i18n import get_local_strings
from janim.logger import log
from janim.render.base import RenderData, Renderer, set_global_uniforms
from janim.typing import SupportsAnim
from janim.utils.config import Config, ConfigGetter, config_ctx_var
from janim.utils.data import ContextSetter
from janim.utils.iterables import resize_preserving_order
from janim.utils.simple_functions import clip

_ = get_local_strings('timeline')


class Timeline(metaclass=ABCMeta):
    '''
    继承该类并实现 :meth:`construct` 方法，以实现动画的构建逻辑

    调用 :meth:`build` 可以得到构建完成的 :class:`Timeline` 对象
    '''

    # region config

    CONFIG: Config | None = None
    '''
    在子类中定义该变量可以起到设置配置的作用，例如：

    .. code-block::

        class Example(Timeline):
            CONFIG = Config(
                font=['Consolas', 'LXGW WenKai Lite']
            )

            def construct(self) -> None:
                ...

    另见：:class:`~.Config`
    '''

    class _WithConfig:
        def __init__(self, cls: type[Timeline]):
            self.cls = cls

            self.lst: list[Config] = []
            for sup in self.cls.mro():
                config: Config | None = getattr(sup, 'CONFIG', None)
                if config is None or config in self.lst:
                    continue
                self.lst.append(config)

            self.lst.reverse()

        def __enter__(self) -> Self:
            lst = [*config_ctx_var.get(), *self.lst]
            self.token = config_ctx_var.set(lst)
            return self

        def __exit__(self, exc_type, exc_value, tb) -> None:
            config_ctx_var.reset(self.token)

    @classmethod
    def with_config(cls) -> _WithConfig:
        '''
        使用定义在 :class:`Timeline` 子类中的 config
        '''
        return cls._WithConfig(cls)

    # endregion

    # region context

    ctx_var: ContextVar[Timeline | None] = ContextVar('Timeline.ctx_var')

    @staticmethod
    def get_context(raise_exc=True) -> Timeline | None:
        '''
        调用该方法可以得到当前正在构建的 :class:`Timeline` 对象

        - 如果在 :meth:`construct` 方法外调用，且 ``raise_exc=True`` （默认），则抛出 :class:`~.TimelineLookupError`
        '''
        obj = Timeline.ctx_var.get(None)
        if obj is None and raise_exc:
            f_back = inspect.currentframe().f_back
            raise TimelineLookupError(
                _('{name} cannot be used outside of Timeline.construct')
                .format(name=f_back.f_code.co_qualname)
            )
        return obj

    # endregion

    # TODO: PlayAudioInfo

    # TODO: SubtitleInfo

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.current_time: float = 0
        self.times_of_code: list[Timeline.TimeOfCode] = []

        self.scheduled_tasks: list[Timeline.ScheduledTask] = []
        # TODO: DEPRECATED?: self.anims
        # TODO: DEPRECATED?: self.display_anims
        self.audio_infos: list[Timeline.PlayAudioInfo] = []
        # TODO: subtitle_infos

        self.pause_points: list[Timeline.PausePoint] = []

        self.time_aligner: TimeAligner = TimeAligner()
        self.item_appearances: defaultdict[Item, Timeline.ItemAppearance] = \
            defaultdict(lambda: Timeline.ItemAppearance(self.time_aligner))

    @abstractmethod
    def construct(self) -> None:
        '''
        继承该方法以实现动画的构建逻辑
        '''
        pass    # pragma: no cover

    def build(self, *, quiet=False, hide_subtitles=False) -> BuiltTimeline:
        '''
        构建动画并返回
        '''
        with self.with_config(), ContextSetter(self.ctx_var, self):

            self.config_getter = ConfigGetter(config_ctx_var.get())
            self.camera = Camera()
            self.track(self.camera)
            self.hide_subtitles = hide_subtitles

            if not quiet:   # pragma: no cover
                log.info(_('Building "{name}"').format(name=self.__class__.__name__))
                start_time = time.time()

            self._build_frame = inspect.currentframe()

            try:
                self.construct()
            finally:
                self._build_frame = None

            if self.current_time == 0:
                self.forward(DEFAULT_DURATION, _record_lineno=False)    # 使得没有任何前进时，产生一点时间，避免除零以及其它问题
                if not quiet:   # pragma: no cover
                    log.info(
                        _('"{name}" did not produce a duration after construction, '
                          'automatically generated a duration of {duration}s')
                        .format(name=self.__class__.__name__, duration=DEFAULT_DURATION)
                    )

            for item, appr in self.item_appearances.items():
                if not appr.stack.has_detected_change():
                    appr.stack.detect_change(item, 0)

            built = BuiltTimeline(self)

            if not quiet:   # pragma: no cover
                elapsed = time.time() - start_time
                log.info(
                    _('Finished building "{name}" in {elapsed:.2f} s')
                    .format(name=self.__class__.__name__, elapsed=elapsed)
                )

        return built

    # region schedule

    @dataclass
    class ScheduledTask:
        '''
        另见 :meth:`~.Timeline.schedule`
        '''
        at: float
        func: Callable
        args: list
        kwargs: dict

    def schedule(self, at: float, func: Callable, *args, **kwargs) -> None:
        '''
        计划执行

        会在进度达到 ``at`` 时，对 ``func`` 进行调用，
        可传入 ``*args`` 和 ``**kwargs``
        '''
        task = Timeline.ScheduledTask(self.time_aligner.align_t(at), func, args, kwargs)
        insort(self.scheduled_tasks, task, key=lambda x: x.at)

    def timeout(self, delay: float, func: Callable, *args, **kwargs) -> None:
        '''
        相当于 `schedule(self.current_time + delay, func, *args, **kwargs)`
        '''
        self.schedule(self.current_time + delay, func, *args, **kwargs)

    # endregion

    # region progress

    @dataclass
    class TimeOfCode:
        '''
        标记 :meth:`~.Timeline.construct` 执行到的代码行数所对应的时间
        '''
        time: float
        line: int

    def forward(self, dt: float = DEFAULT_DURATION, *, _detect_changes=True, _record_lineno=True):
        '''
        向前推进 ``dt`` 秒
        '''
        if dt <= 0:
            raise ValueError(_('dt must be greater than 0'))

        if _detect_changes:
            self.detect_changes_of_all()

        to_time = self.current_time + dt

        while self.scheduled_tasks and self.scheduled_tasks[0].at <= to_time:
            task = self.scheduled_tasks.pop(0)
            self.current_time = task.at
            task.func(*task.args, **task.kwargs)

        self.current_time = to_time

        if _record_lineno:
            self.times_of_code.append(
                Timeline.TimeOfCode(
                    self.current_time,
                    self.get_construct_lineno() or -1
                )
            )

    def forward_to(self, t: float, *, _detect_changes=True) -> None:
        '''
        向前推进到 ``t`` 秒的时候
        '''
        self.forward(t - self.current_time, _detect_changes=_detect_changes)

    def prepare(self, *anims: SupportsAnim, at: float = 0, **kwargs) -> TimeRange:
        self.detect_changes_of_all()
        group = AnimGroup(*anims, at=at + self.current_time, **kwargs)
        group._align_time(self.time_aligner)
        group._time_fixed()

    def play(self, *anims: SupportsAnim, **kwargs) -> TimeRange:
        t_range = self.prepare(*anims, **kwargs)
        self.forward_to(t_range.end, _detect_changes=False)
        return t_range

    @dataclass
    class PausePoint:
        at: float
        at_previous_frame: bool

    def pause_point(
        self,
        *,
        offset: float = 0,
        at_previous_frame: bool = True
    ) -> None:
        '''
        标记在预览界面中，执行到当前时间点时会暂停

        - ``at_previous_frame`` 控制是在前一帧暂停（默认）还是在当前帧暂停
        - ``offset`` 表示偏移多少秒，例如 ``offset=2`` 则是当前位置 2s 后
        - 在 GUI 界面中，可以使用 ``Ctrl+Z`` 快速移动到前一个暂停点，``Ctrl+C`` 快速移动到后一个
        '''
        self.pause_points.append(Timeline.PausePoint(self.current_time + offset, at_previous_frame))

    # endregion

    # TODO: aas

    # TODO: audio_and_subtitle

    # region audio

    @dataclass
    class PlayAudioInfo:
        '''
        调用 :meth:`~.Timeline.play_audio` 的参数信息
        '''
        audio: Audio
        range: TimeRange
        clip_range: TimeRange

    def play_audio(
        self,
        audio: Audio,
        *,
        delay: float = 0,
        begin: float = 0,
        end: float = -1,
        clip: tuple[float, float] | None = None,
    ) -> TimeRange:
        '''
        在当前位置播放音频

        - 可以指定 ``begin`` 和 ``end`` 表示裁剪区段
        - 可以指定在当前位置往后 ``delay`` 秒才开始播放
        - 若指定 ``clip``，则会覆盖 ``begin`` 和 ``end`` （可以将 ``clip`` 视为这二者的简写）

        返回值表示播放的时间段
        '''
        if clip is not None:
            begin, end = clip

        if end == -1:
            end = audio.duration()
        duration = end - begin
        at = self.current_time + delay

        info = Timeline.PlayAudioInfo(audio,
                                      TimeRange(at, at + duration),
                                      TimeRange(begin, end))
        self.audio_infos.append(info)

        return info.range.copy()

    def has_audio(self) -> bool:
        '''
        是否有可以播放的音频
        '''
        return len(self.audio_infos) != 0

    # endregion

    # TODO: region subtitle

    # region ItemAppearance

    class ItemAppearance:
        '''
        包含与物件显示有关的对象

        - ``self.stack`` 即 :class:`~.AnimStack` 对象

        - ``self.visiblility`` 是一个列表，存储物件显示/隐藏的时间点
          - 列表中偶数下标（0、2、...）的表示开始显示的时间点，奇数下标（1、3、...）的表示隐藏的时间点
          - 例如，如果列表中是 ``[3, 4, 8]``，则表示在第 3s 显示，第 4s 隐藏，并且在第 8s 后一直显示
          - 这种记录方式是 :meth:`Timeline.is_visible`、:meth:`Timeline.show`、:meth:`Timeline.hide` 运作的基础

        - ``self.renderer`` 表示所使用的渲染器对象
        '''
        def __init__(self, aligner: TimeAligner):
            self.stack = AnimStack(aligner)
            self.visibility: list[float] = []
            self.renderer: Renderer | None = None
            self.render_disabled: bool = False

        def is_visible_at(self, t: float) -> bool:
            '''
            在 ``t`` 时刻，物件是否可见
            '''
            idx = bisect(self.visibility, t)
            return idx % 2 == 1

        def render(self, data: Item) -> None:
            if self.renderer is not None:
                self.renderer = data.create_renderer()
            self.renderer.render(data)

    # region ItemAppearance.stack

    def track(self, item: Item) -> None:
        '''
        使得 ``item`` 在每次 ``forward`` 和 ``play`` 时都会被自动调用 :meth:`~.Item.detect_change`
        '''
        self.item_appearances[item]

    def track_item_and_descendants(self, item: Item, *, root_only: bool = False) -> None:
        '''
        相当于对 ``item`` 及其所有的后代物件调用 :meth:`track`
        '''
        for subitem in item.walk_self_and_descendants(root_only):
            self.item_appearances[subitem]

    def detect_changes_of_all(self) -> None:
        '''
        检查物件的变化并将变化记录为 :class:`~.Display`
        '''
        for item, appr in self.item_appearances.items():
            appr.stack.detect_change(item, self.current_time)

    def detect_changes(self, items: Iterable[Item]) -> None:
        '''
        检查指定的列表中物件的变化，并将变化记录为 :class:`~.Display`

        （仅检查自身而不包括子物件的）
        '''
        for item in items:
            self.item_appearances[item].stack.detect_change(item, self.current_time)

    def compute_item[T](self, item: T, as_time: float, readonly: bool) -> T:
        '''
        另见 :meth:`~.AnimStack.compute`
        '''
        return self.item_appearances[item].stack.compute(as_time, readonly)

    def item_current[T: Item](self, item: T, *, as_time: float | None = None, root_only: bool = False) -> T:
        '''
        另见 :meth:`~.Item.current`
        '''
        if as_time is None:
            params = updater_params_ctx.get(None)
            if params is not None:
                as_time = params.global_t
        if as_time is None:
            as_time = Animation.global_t_ctx.get(None)

        if as_time is None:
            return item.copy(root_only=root_only)

        root = self.compute_item(item, as_time, False)
        if not root_only:
            assert not root.children and root.stored_children is not None
            root.add(*[self.item_current(sub) for sub in root.stored_children])
            root.stored = False
        return root

    # endregion

    # region ItemAppearance.visibility

    def is_visible(self, item: Item) -> bool:
        '''
        判断特定的物件目前是否可见

        另见：:meth:`show`、:meth:`hide`
        '''
        # 在运行 construct 过程中，params 是 None，返回值表示最后状态是否可见
        params = updater_params_ctx.get(None)
        if params is None:
            return len(self.item_appearances[item].visibility) % 2 == 1

        # 在 updater 的回调函数中，params 不是弄，返回值表示在这时是否可见
        return self.item_appearances[item].is_visible_at(params.global_t)

    def is_displaying(self, item: Item) -> bool:
        from janim.utils.deprecation import deprecated
        deprecated(
            'Timeline.is_displaying',
            'Timeline.is_visible',
            remove=(3, 3)
        )
        return self.is_visible(item)

    def _show(self, item: Item) -> None:
        gaps = self.item_appearances[item].visibility
        if len(gaps) % 2 != 1:
            gaps.append(self.time_aligner.align_t(self.current_time))

    def show(self, *roots: Item, root_only=False) -> None:
        '''
        显示物件
        '''
        for root in roots:
            for item in root.walk_self_and_descendants(root_only):
                self._show(item)

    def _hide(self, item: Item) -> None:
        gaps = self.item_appearances[item].visibility
        if len(gaps) % 2 == 1:
            gaps.append(self.time_aligner.align_t(self.current_time))

    def hide(self, *roots: Item, root_only=False) -> None:
        '''
        隐藏物件
        '''
        for root in roots:
            for item in root.walk_self_and_descendants(root_only):
                self._hide(item)

    def hide_all(self) -> None:
        '''
        隐藏显示中的所有物件
        '''
        for appr in self.item_appearances.values():
            gaps = appr.visibility
            if len(gaps) % 2 == 1:
                gaps.append(self.time_aligner.align_t(self.current_time))

    def cleanup_display(self) -> None:
        from janim.utils.deprecation import deprecated
        deprecated(
            'Timeline.cleanup_display',
            'Timeline.hide_all',
            remove=(3, 3)
        )
        self.hide_all()

    # endregion

    # region lineno

    def get_construct_lineno(self) -> int | None:
        '''
        得到当前在 :meth:`construct` 中执行到的行数
        '''
        frame = inspect.currentframe().f_back
        while frame is not None:
            f_back = frame.f_back

            if f_back is self._build_frame and frame.f_code.co_filename == inspect.getfile(self.__class__):
                return frame.f_lineno

            frame = f_back

        return None     # pragma: no cover

    def get_lineno_at_time(self, time: float):
        '''
        根据 ``time`` 得到对应执行到的行数
        '''
        toc = self.times_of_code
        if not toc:
            return -1

        idx = bisect(toc, time, key=lambda x: x.time)
        idx = clip(idx, 0, len(toc) - 1)
        return toc[idx].line

    # endregion

    # region debug

    @staticmethod
    def fmt_time(t: float) -> str:
        time = round(t, 3)

        minutes = int(time // 60)
        time %= 60

        hours = minutes // 60
        minutes %= 60

        seconds = math.floor(time)
        ms = round((time - seconds) * 1e3)

        times = []
        if hours != 0:
            times.append(f'{hours}h')
        times.append(f'{minutes:>3d}m' if minutes != 0 else ' ' * 4)
        times.append(f'{seconds:>3d}s')
        times.append(f'{ms:>4d}ms' if ms != 0 else ' ' * 6)

        return "".join(times)

    def dbg_time(self, ext_msg: str = '') -> None:  # pragma: no cover
        if ext_msg:
            ext_msg = f'[{ext_msg}]  '

        time = self.fmt_time(self.current_time)

        log.debug(f't={time}  {ext_msg}at construct.{self.get_construct_lineno()}')

    # endregion


# TODO: SourceTimeline


# TODO: SEGMENT_DURATION


# TODO: _LongOptAnimGroup


class BuiltTimeline:
    '''
    运行 :meth:`Timeline.build` 后返回的实例
    '''
    def __init__(self, timeline: Timeline):
        self.timeline = timeline
        self.duration = timeline.current_time

    @property
    def cfg(self) -> Config | ConfigGetter:
        return self.timeline.config_getter

    def get_audio_samples_of_frame(
        self,
        fps: float,
        framerate: int,
        frame: int,
        *,
        count: int = 1
    ) -> np.ndarray:
        '''
        提取特定帧的音频流
        '''
        begin = frame / fps
        end = (frame + count) / fps
        channels = self.cfg.audio_channels

        output_sample_count = math.floor(end * framerate) - math.floor(begin * framerate)
        if channels == 1:
            result = np.zeros(output_sample_count, dtype=np.int16)
        else:
            result = np.zeros((output_sample_count, channels), dtype=np.int16)

        for info in self.timeline.audio_infos:
            if end < info.range.at or begin > info.range.end:
                continue

            audio = info.audio

            frame_begin = int((begin - info.range.at + info.clip_range.at) * audio.framerate)
            frame_end = int((end - info.range.at + info.clip_range.at) * audio.framerate)

            clip_begin = max(0, int(audio.framerate * info.clip_range.at))
            clip_end = min(audio.sample_count(), int(audio.framerate * info.clip_range.end))

            left_blank = max(0, clip_begin - frame_begin)
            right_blank = max(0, frame_end - clip_end)

            data = audio._samples.data[max(clip_begin, frame_begin): min(clip_end, frame_end)]

            if left_blank != 0 or right_blank != 0:
                if channels == 1:
                    data = np.concatenate([
                        np.zeros(left_blank, dtype=np.int16),
                        data,
                        np.zeros(right_blank, dtype=np.int16)
                    ])
                else:
                    # channels = data.shape[1]
                    data = np.concatenate([
                        np.zeros((left_blank, channels), dtype=np.int16),
                        data,
                        np.zeros((right_blank, channels), dtype=np.int16)
                    ])

            result += resize_preserving_order(data, output_sample_count)

        return result

    def render_all(self, ctx: mgl.Context, global_t: float) -> None:
        '''
        渲染所有可见物件
        '''
        timeline = self.timeline
        global_t = timeline.time_aligner.align_t_for_render(global_t)
        try:
            with ContextSetter(Animation.global_t_ctx, global_t),   \
                 ContextSetter(Timeline.ctx_var, self.timeline),    \
                 self.timeline.with_config():
                camera = timeline.compute_item(timeline.camera, global_t, True)
                camera_info = camera.points.info
                anti_alias_radius = self.cfg.anti_alias_width / 2 * camera_info.scaled_factor

                set_global_uniforms(
                    ctx,
                    ('JA_CAMERA_SCALED_FACTOR', camera_info.scaled_factor),
                    ('JA_VIEW_MATRIX', camera_info.view_matrix.T.flatten()),
                    ('JA_FIXED_DIST_FROM_PLANE', camera_info.fixed_distance_from_plane),
                    ('JA_PROJ_MATRIX', camera_info.proj_matrix.T.flatten()),
                    ('JA_FRAME_RADIUS', camera_info.frame_radius),
                    ('JA_ANTI_ALIAS_RADIUS', anti_alias_radius)
                )

                with ContextSetter(Renderer.data_ctx, RenderData(ctx=ctx,
                                                                 camera_info=camera_info,
                                                                 anti_alias_radius=anti_alias_radius)):
                    # 遍历所有物件，筛选出参与渲染的
                    render_items = [
                        (item, appr)
                        for item, appr in timeline.item_appearances.items()
                    ]
                    # 反向遍历一遍所有物件，这是为了让例如 Transform 之类的效果标记原有的物件不进行渲染
                    # （会把所应用的物件的 render_disabled 置为 True，所以在下面可以判断这个变量过滤掉它们）
                    for item, _ in reversed(render_items):
                        item._mark_render_disabled()
                    # 剔除被标记 render_disabled 的物件，得到 render_items_final，并按深度排序
                    render_items_final: list[tuple[Item, Timeline.ItemAppearance]] = []
                    for item, appr in render_items:
                        if appr.render_disabled:
                            appr.render_disabled = False    # 重置，因为每次都要重新标记
                            continue
                        if not appr.is_visible_at(global_t):
                            continue
                        data = appr.stack.compute(global_t, True)
                        render_items_final.append((data, appr))
                    render_items_final.sort(key=lambda x: x[0].depth, reverse=True)
                    # 渲染
                    for data, appr in render_items_final:
                        appr.render(data)

        except Exception:
            traceback.print_exc()

    # TODO: anim_on

    # TODO: render_all

    # TODO: capture
    # TODO: janim.render.base.create_context
