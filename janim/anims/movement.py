
from typing import Callable

import numpy as np

from janim.anims.updater import DataUpdater, UpdaterParams
from janim.items.item import Item
from janim.items.points import Points
from janim.items.vitem import VItem
from janim.typing import Vect


class Homotopy(DataUpdater):
    def __init__(
        self,
        item: Item,
        homotopy: Callable[[float, float, float, float], Vect],
        *,
        duration: float = 3.,
        root_only: bool = False,
        **kwargs
    ):
        '''
        一个从 (x, y, z, t) 到 (x’, y’, z’) 的函数

        t 的取值范围是 [0, 1]，表示动画进度
        '''
        self.homotopy = homotopy
        super().__init__(
            item,
            self.updater,
            duration=duration,
            root_only=root_only,
            **kwargs
        )

    def updater(self, data: Item, p: UpdaterParams) -> None:
        if not isinstance(data, Points):
            return

        def fn(point: np.ndarray) -> Vect:
            return self.homotopy(*point, p.alpha)

        # 即使不传入 root_only=True 其实也不会影响子物件
        data.points.apply_point_fn(fn, root_only=True)


class ComplexHomotopy(Homotopy):
    def __init__(
        self,
        item: Item,
        complex_homotopy: Callable[[complex, float], complex],
        **kwargs
    ):
        '''
        与 Homotopy 类似，区别是用复数描述坐标
        '''
        def homotopy(x, y, z, t):
            c = complex_homotopy(complex(x, y), t)
            return (c.real, c.imag, z)

        super().__init__(item, homotopy, **kwargs)


# TODO: PhaseFlow


class MoveAlongPath(DataUpdater):
    def __init__(
        self,
        item: Item,
        path: VItem,
        *,
        root_only: bool = False,
        **kwargs
    ):
        self.path = path
        if root_only:
            self.center = item(Points).points.self_box.center
        else:
            self.center = item(Points).points.box.center

        super().__init__(
            item,
            self.updater,
            root_only=root_only,
            **kwargs
        )

    def updater(self, data: Item, p: UpdaterParams) -> None:
        if not isinstance(data, Points):
            return
        data.points.shift(self.path.points.pfp(p.alpha) - self.center)
