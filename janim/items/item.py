from __future__ import annotations
from typing import Callable, Any, TypeVar
from janim.typing import Self, Vect, VectArray

from functools import wraps
import itertools as it
import copy
import numpy as np
import inspect
import sys

from janim.utils.unique_nparray import UniqueNparray

from janim.logger import log

'''
继承 Item 及有关类的注意事项：
    - 完成对 copy 的继承，使得子类的数据能被正常复制
        - 代码结构：

        def copy(self) -> Self:
            copy_item = super().copy()

            # ===========
            # 有关数据复制
            # ===========

            return copy_item


Inheriting from Item and related classes:
    - Ensure inheritance from copy for proper data copying.
        - Code structure:

        def copy(self) -> Self:
            copy_item = super().copy()

            # ===========
            # Data copying
            # ===========

            return copy_item
'''

class Item:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.parents: list[Item] = []
        self.subitems: list[Item] = []
        self.markers: list[Points] = []  # TODO: Item.markers

        self.refresh_required: dict[str, bool] = {}
        self.refresh_stored_data: dict[str, Any] = {}

    class Signal:
        '''
        一般用于在 `func` 造成影响后，需要对其它数据进行更新时进行响应

        当 `func` 被该类修饰，使用 `Class.func.emit(self)` 后
        - 会调用所有被 `func.slots()` 修饰的方法
        - 会为所有被 `func.refresh()` 修饰的方法调用 `mark_refresh_required`
        - 可以在上述方法中传入 `key` 参数以区分调用
        - `func.refresh()` 除了 `key` 之外，还可以传入 `recurse_down/up`
        - `emit` 方法可以传入额外的参数给被调用的 `slots`

        注意：
        - 被 `func.slot()` 或 `func.refresh()` 修饰的方法需要与 `func` 在同一个类或者其子类中

        ---
        
        Generally used to respond to updates in other data after an impact caused by `func`.

        When a method `func` is decorated with this class and used as `Class.func.emit(self)`:
        - It will invoke all methods decorated with `func.slots()`.
        - It will call `mark_refresh_required` for all methods decorated with `func.refresh()`.
        - You can pass a `key` parameter in the above methods to distinguish the calls.
        - Besides `key`, `func.refresh()` can also take `recurse_down/up` as parameters.
        - The `emit` method can pass additional arguments to the invoked `slots`.

        Note:
        - Methods decorated with `func.slot()` or `func.refresh()` need to be in the same class or its subclass.

        ---

        #### 例 | Example:
        ```python
        class User(Item):
            def __init__(self, name: str):
                super().__init__()
                self.name = name
                self.msg = ''
            
            @Item.Signal
            def set_msg(self, msg: str) -> None:
                self.msg = msg
                User.set_msg.emit(self)
            
            @set_msg.slot()
            def notifier(self) -> None:
                print("User's message changed")

            @set_msg.refresh()
            @Item.register_refresh_required
            def get_test(self) -> str:
                return f'[{self.name}] {self.msg}'
            
        user = User('jkjkil')
        user.set_msg('hello')   # Output: User's message changed
        print(user.get_text())  # Output: [jkjkil] hello
        ```
        还可以参考 | See also：
        - `janim.items.item.Item.get_points`
        - `test.items.item_test.ItemTest.test_signal_with_inherit`
        '''
        def __init__(self, func: Callable):
            self.func = func

            Key = FullQualname = str
            Slots = list[Callable]
            RefreshSlots = list[Callable, bool, bool]

            self.slots: dict[
                Key,
                dict[
                    FullQualname, 
                    tuple[
                        Slots,
                        RefreshSlots
                    ]
                ]
            ] = {}
        
        def __get__(self, instance, owner):
            return self if instance is None else self.func.__get__(instance, owner)
        
        @staticmethod
        def get_cls_full_qualname_from_fback() -> str:
            cls_locals = inspect.currentframe().f_back.f_back.f_locals
            module = cls_locals['__module__']
            qualname = cls_locals['__qualname__']
            return f'{module}.{qualname}'
        
        @staticmethod
        def get_cls_full_qualname(cls: type) -> str:
            return f'{cls.__module__}.{cls.__qualname__}'
        
        def ensure_slots_list_available(self, key: str, full_qualname: str) -> None:
            if key not in self.slots:
                self.slots[key] = { full_qualname: ([], []) }
            
            elif full_qualname not in self.slots[key]:
                self.slots[key][full_qualname] = ([], [])
        
        def slot(self, *, key: str = ''):
            def decorator(func: Callable) -> Callable:
                full_qualname = self.get_cls_full_qualname_from_fback()
                self.ensure_slots_list_available(key, full_qualname)
                self.slots[key][full_qualname][0].append(func)

                return func

            return decorator
        
        def refresh(self, *, recurse_down: bool = False, recurse_up: bool = False, key: str = ''):
            def decorator(func: Callable) -> Callable:
                full_qualname = self.get_cls_full_qualname_from_fback()
                self.ensure_slots_list_available(key, full_qualname)
                self.slots[key][full_qualname][1].append((func, recurse_down, recurse_up))

                return func

            return decorator
        
        def emit(self, item: Item, *args, key: str = '', **kwargs):
            try:
                all_slots = self.slots[key]
            except KeyError:
                return
            
            for cls in item.__class__.mro():
                try:
                    slots, refresh_slots = all_slots[self.get_cls_full_qualname(cls)]
                except KeyError: 
                    continue

                for func in slots:
                    func(item, *args, **kwargs)
                
                for func, recurse_down, recurse_up in refresh_slots:
                    item.mark_refresh_required(func, recurse_down=recurse_down, recurse_up=recurse_up)

    #region refreshing
        
    '''
    refresh 相关方法
    用于在需要时才进行值的重新计算，提升性能
    
    当一个方法 self.func 被 `register_refresh_required` 修饰后
    会记忆 self.func 被调用后的返回值，并在之后的调用中直接返回该值，而不对 self.func 进行真正的调用
    需要 `mark_refresh_required(self.func)` 才会对 self.func 重新调用以更新返回值

    例如，`get_family` 方法不会每次都进行计算
    只有在 `add` 或 `remove` 执行后，才会将 `get_family` 标记为需要更新
    使得在下次调用 `get_family` 才重新计算结果并返回

    可参考 `test.items.item_test.ItemTest.test_refresh_required`


    Methods related to refreshing
    Used to recalculate values only when necessary for performance improvement

    When a method self.func is decorated with `register_refresh_required`,
    it memorizes the return value of self.func and directly returns that value in subsequent calls,
    without actually calling self.func. 
    `mark_refresh_required(self.func)` is needed to trigger a reevaluation of self.func and update the return value.

    For example, the `get_family` method does not recalculate every time.
    It is only marked for update after `add` or `remove` is executed,
    making it recalculated and returning the result the next time `get_family` is called.

    See `test.items.item_test.ItemTest.test_refresh_required` for an example.
    '''

    def register_refresh_required(func: Callable) -> Callable:
        '''
        修饰器，标记方法在需要更新时才重新进行计算

        Decorator, marks a method to recalculate only when necessary.
        '''
        name = func.__name__

        @wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                is_required = self.refresh_required[name]
            except KeyError:
                is_required = True

            if is_required:
                ret = self.refresh_stored_data[name] = func(self, *args, **kwargs)
                self.refresh_required[name] = False
                return ret
            return self.refresh_stored_data[name]
        
        return wrapper
    
    def mark_refresh_required(self, func: Callable, *, recurse_down: bool = False, recurse_up: bool = False) -> Self:
        '''
        标记指定的 `func` 需要进行更新

        `recurse_down`: 是否递归调用子物件的该方法
        `recurse_up`: 是否递归调用父物件的该方法

        
        Marks the specified `func` for an update.

        `recurse_down`: Whether to recursively call the method on subitems.
        `recurse_up`: Whether to recursively call the method on parents.
        '''
        self.refresh_required[func.__name__] = True
        if recurse_down:
            for item in self.subitems:
                item.mark_refresh_required(func, recurse_down=True)
        if recurse_up:
            for item in self.parents:
                item.mark_refresh_required(func, recurse_up=True)

    #endregion
                
    #region relation
                
    '''
    两个物件之间可以建立 parent-subitem 双向关系
    主要用于 mark_refresh_required 以及一些属性的传递
    并不与渲染顺序直接相关

    Bidirectional parent-subitem relationship can be established between two items.
    Mainly used for `mark_refresh_required` and the transfer of some properties.
    It is not directly related to the rendering order.
    '''

    @register_refresh_required
    def get_family(self) -> list[Item]:
        '''
        获取该物件的所有子物件，即包括自己和子物件的子物件
        
        Get all subitems of this item, including itself and the subitems of its subitems.
        '''
        return [self, *it.chain(*[item.get_family() for item in self.subitems])]

    def add(self, *items: Item) -> Self:
        '''
        向该物件添加子物件
        
        Add subitems to this item.
        '''
        for item in items:
            # 理论上这里判断 item not in self.subitems 就够了
            # 但是防止有被私自修改 self.parents 以及 self.subitems 的可能
            # 所以这里都判断了
            if item not in self.subitems:
                self.subitems.append(item)
            if self not in item.parents:
                item.parents.append(self)
            
        self.mark_refresh_required(Item.get_family, recurse_up=True)
        return self
    
    def remove(self, *items: Item) -> Self:
        '''
        从该物件移除子物件
        
        Remove subitems from this item.
        '''
        for item in items:
            # 理论上这里判断 item in self.subitems 就够了
            # 原因同 add
            try:
                self.subitems.remove(item)
            except ValueError: ...
            try:
                item.parents.remove(self)
            except ValueError: ...
        
        self.mark_refresh_required(Item.get_family, recurse_up=True)
        return self
    
    def __getitem__(self, value) -> Self:   # 假装返回 Self，用于类型提示
        if isinstance(value, slice):
            return GroupClass(*self.subitems[value])
        return self.subitems[value]
    
    def __iter__(self):
        return iter(self.subitems)
    
    def __len__(self):
        return len(self.subitems)
    
    #endregion

    #region 快速操作 | quick operations

    @property
    def for_all(self) -> Self:  # 假装返回 Self，方便代码补全
        '''
        对自己 `get_family()` 的所有子物件进行操作
        
        Operates on all items obtained from `get_family()`.
        '''
        return BatchOp(*self.get_family())

    @property
    def for_all_p(self) -> Self:
        '''
        同 `for_all`，但是调用的返回值以 `(item, retval)` 为单位
        
        Same as `for_all`, but the returned values are paired with `(item, retval)`.
        '''
        return BatchOp(*self.get_family(), paired=True)
    
    @property
    def for_all_except_self(self) -> Self:
        '''
        对不包括自己的 `get_family()` 的所有子物件进行操作
        
        Operatres on all items obtained from `get_family()`, excluding the item itself.
        '''
        return BatchOp(*self.get_family()[1:])
    
    @property
    def for_all_except_self_p(self) -> Self:
        '''
        同 `for_all_except_self`，但是调用的返回值以 `(item, retval)` 为单位
        
        Same as `for_all_except_self`, but the returned values are paired with `(item, retval)`.
        '''
        return BatchOp(*self.get_family()[1:], paired=True)
    
    @property
    def for_sub(self) -> Self:
        '''
        对 `subitems` 中的物件进行操作
        
        Operates on subitems.
        '''
        return BatchOp(*self.subitems)
    
    @property
    def for_sub_p(self) -> Self:
        '''
        同 `for_sub`，但是调用的返回值以 `(item, retval)` 为单位
        
        Same as `for_sub`, but the returned values are paired with `(item, retval)`.
        '''
        return BatchOp(*self.subitems, paired=True)

    #endregion

    #region 数据复制 | data copying

    def copy(self) -> Self:
        copy_item = copy.copy(self)
        
        # relation
        copy_item.parents = []
        copy_item.subitems = []
        copy_item.add(*[m.copy() for m in self])
        
        # TODO: copy: markers

        # other
        self.refresh_required.clear()       # 清空，使得所有操作都要重新计算数据
                                            # Clear, so that all opeartions require recalculating data.
        self.refresh_stored_data.clear()    # 既然 refresh_required 清空了，那么这个就顺便也清空吧，反正也要重新计算的
                                            # Since refresh_required is cleared, clear this too, as it needs to be recalculated.

        return copy_item

    def __mul__(self, times: int) -> Self:  # 假装返回 Self，方便代码补全 | Pretends to return Self for code completion.
        '''
        将自身复制 `times` 次，组合到一个 `Group` 中

        Duplicate itself `times` times and group them into a new `Group`.
        '''
        return GroupClass(
            *(self.copy() for _ in range(times))
        )

    #endregion

class Points(Item):
    def __init__(
        self, 
        *args, 
        points: VectArray = np.array([]), 
        **kwargs
    ):
        super().__init__(*args, **kwargs)

        self.points = UniqueNparray(points)

    def copy(self) -> Self:
        copy_item = super().copy()

        copy_item.points = UniqueNparray()
        copy_item.points.data = self.points.data

    #region points
        
    def get_points(self) -> VectArray:
        return self.points.data
    
    def get_all_points(self) -> VectArray:
        return np.vstack(self.for_all.get_points())

    @Item.Signal
    def set_points(self, points: VectArray) -> Self:
        '''
        设置点坐标数据，每个坐标点都有三个分量
        
        使用形如 `set_points([[1.5, 3, 2], [2, 1.5, 0]])` 的形式

        Set point coordinate data, with each point having three components.
        
        Use a format like `set_points([[1.5, 3, 2], [2, 1.5, 0]])`.
        '''
        if not isinstance(points, np.ndarray):
            points = np.array(points)
    
        assert(points.ndim == 2)
        assert(points.shape[1] == 3)

        cnt_changed = len(points) != len(self.points.data)

        self.points.data = points

        if cnt_changed:
            Points.set_points.emit(self, key='count')
        Points.set_points.emit(self)

        return self
    
    def clear_points(self) -> Self:
        self.set_points(np.zeros((0, 3)))
        return self
    
    def append_points(self, points: VectArray) -> Self:
        '''
        追加点坐标数据，每个坐标点都有三个分量

        使用形如 `append_points([[1.5, 3, 2], [2, 1.5, 0]])` 的形式

        Append point coordinate data, with each point having three components.

        Use a format like `append_points([[1.5, 3, 2], [2, 1.5, 0]])`.
        '''
        self.set_points(np.vstack([
            self.get_points(),
            points
        ]))
        return self
    
    @Item.Signal
    def reverse_points(self) -> Self:
        '''使点倒序 | reverse the order of points'''
        self.set_points(self.get_points()[::-1])
        Points.reverse_points.emit(self)
        return self
    
    # TODO: resize_points

    def points_count(self) -> int:
        return len(self.get_points())
    
    def has_points(self) -> bool:
        return self.points_count() > 0
    
    def get_start(self) -> Vect:
        self.throw_error_if_no_points()
        return self.get_points()[0].copy()

    def get_end(self) -> Vect:
        self.throw_error_if_no_points()
        return self.get_points()[-1].copy()
    
    def throw_error_if_no_points(self) -> None:
        if not self.has_points():
            # TODO: i18n
            message = "Cannot call Item.{} " +\
                      "for a Item with no points"
            caller_name = sys._getframe(1).f_code.co_name
            raise Exception(message.format(caller_name))

    #endregion

class GroupClass(Item):
    def __init__(self, *items: Item, **kwargs):
        super().__init__(**kwargs)
        self.add(*items)

T = TypeVar('T', bound=Item)

# 该方法用于方便类型检查以及补全提示
# 在执行时和直接调用 GroupClass 没有区别
def Group(*items: T) -> Item | T:
    '''
    将 `items` 打包为一个 `Group`，方便属性的设定
    
    Pack `items` into a `Group` for convenient property setting.
    '''
    return GroupClass(*items)

class BatchOp:
    '''
    批量操作，对该实例的调用都会作用到传入的物件上

    如果对每个物件的调用返回的都是 `None` 或者物件本身，
    那么对该实例的调用会返回 `self`

    如果对物件的调用存在其它返回值，
    那么会将每个物件调用后的返回值放入列表中返回

    如果 `paired` 为 `False`，
    那么物件有返回值时则返回 `[val1, val2, ...]`，
    否则返回 `[(item1, val1), (item2, val2), ...]`


    Batch operation, all calls to this instance will apply to the passed items.

    If every call to an item returns `None` or the item itself,
    calling this instance will return `self`.

    If there are other return values for the item calls,
    each return value will be put into a list and returned.

    If `paired` is `False`,
    and items has a return values, it will return `[val1, val2, ...]`,
    otherwise, it will return `[(item1, val1), (item2, val2), ...]`.
    '''
    def __init__(self, *items: Item, paired: bool = False):
        self.items = items
        self.paired = paired
    
    def __getattr__(self, method_name: str):
        def func(*args, **kwargs):
            ret_list = []
            found = False

            # 遍历物件进行方法调用
            # Interate over items for method call
            for item in self.items:
                if not hasattr(item, method_name):
                    continue
                attr = getattr(item, method_name)

                if not callable(attr):
                    continue
                found = True
                
                ret = attr(*args, **kwargs)
                if ret is not None and ret is not item:
                    ret_list.append((item, ret) if self.paired else ret)

            # 如果没有任何物件调用了方法，那么进行警告
            # If no item called the method, issue a warning
            if not found:
                frame = inspect.currentframe().f_back
                # TODO: i18n
                log.warning(f'[{frame.f_code.co_filename}:{frame.f_lineno}] 没有物件拥有 `{method_name}` 方法，调用没有产生效果')

            return ret_list or self
    
        return func
                    


