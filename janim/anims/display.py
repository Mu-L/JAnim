from typing import TYPE_CHECKING

from janim.anims.animation import Animation, RenderCall

if TYPE_CHECKING:   # pragma: no cover
    from janim.items.item import Item


class Display(Animation):
    '''
    在指定的时间区间上显示物件
    '''
    def __init__(self, item: 'Item', root_only=False, **kwargs):
        super().__init__(**kwargs)
        self.item = item
        self.root_only = root_only

    def anim_on(self, local_t: float) -> None:
        super().anim_on(local_t)

        self.set_render_call_list([
            RenderCall(
                self.item.depth.current(),
                self.item.render
            )
        ])
