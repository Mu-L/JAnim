
from __future__ import annotations

import copy
import os
import subprocess as sp
from typing import Iterable, Self

import numpy as np

from janim.exception import EXITCODE_FFMPEG_NOT_FOUND, ExitException
from janim.logger import log
from janim.utils.config import Config
from janim.utils.iterables import resize_with_interpolation
from janim.utils.simple_functions import clip
from janim.utils.unique_nparray import UniqueNparray


class Audio:
    '''
    不建议使用该类处理多声道音频，因为该类读取时仅保留单声道
    '''
    def __init__(self, filepath: str, begin: float = -1, end: float = -1, **kwargs):
        super().__init__(**kwargs)
        self._samples = UniqueNparray(dtype=np.int16)
        self.framerate = 0
        self.filepath = ''
        self.filename = ''
        self.read(filepath, begin, end)

    def copy(self) -> Self:
        copy_audio = copy.copy(self)
        copy_audio._samples = self._samples.copy()
        return copy_audio

    def read(
        self,
        filepath: str,
        begin: float = -1,
        end: float = -1
    ) -> Self:
        '''
        从文件中读取音频

        可以指定 ``begin`` 和 ``end`` 来截取音频的一部分
        '''
        command = [
            Config.get.ffmpeg_bin,
            '-vn',
            '-i', filepath,
        ]
        if begin != -1:
            command += ['-ss', str(begin)]  # clip from
        if end != -1:
            command += ['-to', str(end)]    # clip to

        command += [
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ar', str(Config.get.audio_framerate),     # framerate & samplerate
            '-ac', '1',
            '-loglevel', 'error',
            '-',    # output to a pipe
        ]

        try:
            # TODO: support more sampwidth
            # TODO: fix ByteOrder
            with sp.Popen(command, stdout=sp.PIPE) as reading_process:
                self._samples.data = np.frombuffer(reading_process.stdout.read(), dtype=np.int16)
                self.framerate = Config.get.audio_framerate
                self.filepath = filepath
                self.filename = os.path.basename(filepath)

        except FileNotFoundError:
            log.error('无法读取音频，需要安装 ffmpeg 并将其添加到环境变量中')
            raise ExitException(EXITCODE_FFMPEG_NOT_FOUND)

        return self

    def sample_count(self) -> int:
        '''
        所有采样点的数量
        '''
        return len(self._samples._data)

    def duration(self) -> float:
        '''
        持续时间
        '''
        return self.sample_count() / self.framerate

    def clip(self, begin: float = 0, end: float = -1) -> Self:
        '''
        裁剪音频

        - 保留 ``begin`` 到 ``end`` 之间的部分
        - 若 ``begin`` 缺省，则表示从最开始
        - 若 ``end`` 缺省(``-1``)，则表示到最末尾
        '''
        frame_begin = clip(int(begin * self.framerate), 0, self.sample_count())
        if end == -1:
            frame_end = self.sample_count()
        else:
            frame_end = clip(int(end * self.framerate), 0, self.sample_count())
        self._samples.data = self._samples._data[frame_begin:frame_end]

        return self

    def mul(self, value: float | Iterable[float]) -> Self:
        '''
        乘以给定的 ``value``，``value`` 可以含有多个元素（比如一个列表）

        例如：

        - ``audio.mul(0.5)`` 可以使音高减半
        - ``audio.mul([1, 0])`` 可以使开始时最强，结束时最弱
        - ``audio.mul(np.sin(np.linspace(0, 2 * np.pi, audio.sample_count())))`` 可以使音高随时间乘以 sin 函数的一个周期
        '''
        if isinstance(value, Iterable):
            value = resize_with_interpolation(value, self.sample_count())
        self._samples.data = self._samples.data * value

        return self

    def fade_in(self, duration: float) -> Self:
        '''
        应用 ``duration`` 秒的淡入
        '''
        frames = int(self.framerate * duration)
        data = self._samples.data
        data[:frames] = (data[:frames] * np.linspace(0, 1, frames)).astype(np.int16)
        self._samples.data = data

        return self

    def fade_out(self, duration: float) -> Self:
        '''
        应用 ``duration`` 秒的淡出
        '''
        frames = int(self.framerate * duration)
        data = self._samples.data
        data[-frames:] = (data[-frames:] * np.linspace(1, 0, frames)).astype(np.int16)
        self._samples.data = data