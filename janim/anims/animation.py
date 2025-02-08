from __future__ import annotations

from dataclasses import dataclass
from typing import Self, overload

from janim.constants import FOREVER
from janim.typing import ForeverType
from janim.utils.rate_functions import RateFunc, linear, smooth
from janim.items.item import Item

ALIGN_EPSILON = 1e-6
QUERY_OFFSET = 1e-5


class Animation:
    '''
    动画基类

    - 创建一个从 ``at`` 持续至 ``at + duration`` 的动画
    - ``duration`` 可以是 ``FOREVER``
      （一般用于 :class:`~.Display`，
      以及特殊情况下的 :class:`DataModifier` 等，
      但是 :class:`~.AnimGroup` 及其衍生类不能传入 ``FOREVER``）
    - 指定 ``rate_func`` 可以设定插值函数，默认为 :meth:`janim.utils.rate_funcs.smooth` 即平滑插值
    '''
    # TODO: label_color

    def __init__(
        self,
        *,
        at: float = 0,
        duration: float | ForeverType = 1.0,
        rate_func: RateFunc = smooth
    ):
        # 用于在 AnimGroup 中标记子动画是否都对齐；
        # 对于单个动画来说肯定是对齐的，默认为 True，而在 AnimGroup 中有可能是 False
        # 关于 is_aligned 的计算请参见 AnimGroup.__init__ 代码内的注释
        self.is_aligned = True

        # 意即“覆盖先前的动画”
        # 把该值置为 True 表示该动画不依赖先前动画的效果，使得进行计算时可以直接从该动画开始而不用考虑更前面的动画效果
        # 并且如果在 AnimStack 中没有后继动画，AnimStack 会直接使用 .data_orig 作为结果，而不调用 .apply
        # 例如，该值会被 Display 置为 True，因为 Display 不基于更前面的动画
        self._cover_previous_anims = False

        # 用于标记该动画的全局时间区段
        self.t_range = TimeRange(
            at,
            FOREVER if duration is FOREVER else at + duration
        )

        # 传给该动画对象的 rate_func
        self.rate_func = rate_func

        # 该动画及父动画的 rate_func 组成的列表
        self.rate_funcs = [] if rate_func is linear else [rate_func]

    def __anim__(self) -> Self:
        return self

    def shift_range(self, delta: float) -> Self:
        '''
        以 ``delta`` 的变化量移动时间区段
        '''
        self.t_range.shift(delta)

    def scale_range(self, k: float) -> Self:
        '''
        以 ``k`` 的倍率缩放时间区段（相对于 ``t=0`` 进行缩放）
        '''
        self.t_range.scale(k)

    def _attach_rate_func(self, rate_func: RateFunc) -> None:
        self.rate_funcs.insert(0, rate_func)

    def _align_time(self, aligner: TimeAligner) -> None:
        aligner.align(self)

    def _time_fixed(self) -> None:
        '''
        由子类实现，用于确定该动画的行为，并可用于该对象内容的初始化
        '''
        pass

    # TODO: anim_on

    # TODO: get_alpha_on_global_t

    # TODO: is_visible

    # TODO: global_t_ctx

    # TODO: anim_on_alpha


class ItemAnimation(Animation):
    def __init__(self, item: Item, **kwargs):
        super().__init__(**kwargs)
        self.item = item

    def _time_fixed(self):
        from janim.anims.timeline import Timeline
        timeline = Timeline.get_context()
        timeline.anim_stacks[self.item].append(self)

    @dataclass
    class ApplyParams:
        global_t: float
        anims: list[ItemAnimation]
        index: int

    @overload
    def apply(self, data: Item, p: ApplyParams) -> None: ...
    @overload
    def apply(self, data: None, p: ApplyParams) -> Item: ...

    def apply(self, data, params):
        '''
        将 ``global_t`` 时的动画效果作用到 ``data`` 上

        其中

        - 对于 :class:`~.Display` 而言，``data`` 是 ``None``，返回值是 :class:`~.Item` 对象
        - 而对于其它大多数的而言，``data`` 是前一个动画作用的结果，返回值是 ``None``
        '''
        pass


@dataclass
class TimeRange:
    '''
    标识了从 ``at`` 开始，到 ``end`` 结束的时间区段

    ``end`` 也可以是 ``FOREVER``
    '''

    at: float
    '''时间区段的开始时刻'''

    end: float | ForeverType
    '''时间区段的结束时刻'''

    @property
    def duration(self) -> float:
        '''
        时间区段的时长，即 ``end - at``，如果 ``end=FOREVER`` 则抛出 ``AssertionError``

        另见 :meth:`num_duration`
        '''
        assert self.end is not FOREVER
        return self.end - self.at

    @property
    def num_duration(self) -> float:
        '''
        - 当 ``end`` 不是 ``FOREVER`` 时，与 :meth:`duration` 一致

        - 当 ``end`` 是 ``FOREVER`` 时，此时返回 ``0``

        （这用于 :class:`~.AnimGroup` 对 ``end=FOREVER`` 的子动画的处理，也就是把这种子动画当成 ``end=at`` 来计算时间）
        '''
        return 0 if self.end is FOREVER else self.duration

    @property
    def num_end(self) -> float:
        '''
        - 当 ``end`` 不是 ``FOREVER`` 时，此时返回 ``end``

        - 当 ``end`` 是 ``FOREVER`` 时，此时返回 ``0``

        （这用于 :class:`~.AnimGroup` 对 ``end=FOREVER`` 的子动画的处理，也就是把这种子动画当成 ``end=at`` 来计算时间）
        '''
        return self.at if self.end is FOREVER else self.end

    def set(self, at: float, end: float | ForeverType) -> None:
        '''
        设置该时间区段的范围
        '''
        self.at = at
        self.end = end

    def shift(self, delta: float) -> None:
        '''
        以 ``delta`` 的变化量移动时间区段
        '''
        self.at += delta
        if self.end is not FOREVER:
            self.end += delta

    def scale(self, k: float) -> None:
        '''
        以 ``k`` 的倍率缩放时间区段（相对于 ``t=0`` 进行缩放）
        '''
        self.at *= k
        if self.end is not FOREVER:
            self.end *= k

    def copy(self) -> TimeRange:
        return TimeRange(self.at, self.end)

    def __eq__(self, other: TimeRange) -> bool:
        return self.at == other.at and self.end == other.end


class TimeAligner:
    '''
    由于浮点数精度的问题，有可能出现比如原本设计上首尾相连的两个动画，却出现判定的错位

    该类用于将相近的浮点数归化到同一个值，使得 :class:`TimeRange` 区间严丝合缝
    '''
    def __init__(self):
        self.recorded_times = []

    def align(self, anim: Animation) -> None:
        '''
        归化 ``anim`` 的时间区段，
        即分别对 ``.t_range.at`` 和 ``.t_range.end`` 进行 :meth:`align_t` 的操作
        '''
        rg = anim.t_range
        rg.at = self.align_t(rg.at)
        if rg.end is not FOREVER:
            rg.end = self.align_t(rg.end)

    def align_t(self, t: float) -> float:
        '''
        对齐时间 `t`，确保相近的时间点归化到相同的值，返回归化后的时间值
        '''
        # 因为在大多数情况下，最新传入的 t 总是出现在列表的最后，所以倒序查找
        for i, recorded_t in enumerate(reversed(self.recorded_times)):
            # 尝试归化到已有的值
            if abs(t - recorded_t) < ALIGN_EPSILON:
                return recorded_t
            # 尝试插入到中间位置
            if t > recorded_t:
                # len - 1 - i 是 recorded_t 的位置，所以这里用 len - i 表示插入到其后面
                idx = len(self.recorded_times) - i
                self.recorded_times.insert(idx, t)
                return t

        # 循环结束表明所有已记录的都比 t 大，所以将 t 插入到列表开头
        self.recorded_times.insert(0, t)
        return t
