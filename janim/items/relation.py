from __future__ import annotations

from typing import Callable, Generic, TypeVar, Generator, Type

import janim.utils.refresh as refresh
from janim.typing import Self
from janim.utils.signal import Signal

GRelT = TypeVar('GRelT', bound='Relation')
RelT = TypeVar('RelT', bound='Relation')


class Relation(Generic[GRelT], refresh.Refreshable):
    '''
    定义了有向无环图的包含关系以及一些实用操作

    也就是，对于每个对象：

    - ``self.parents`` 存储了与其直接关联的父对象
    - ``self.children`` 存储了与其直接关联的子对象
    - 使用 :meth:`add()` 建立对象间的关系
    - 使用 :meth:`remove()` 取消对象间的关系
    - :meth:`ancestors()` 表示与其直接关联的祖先对象（包括父对象，以及父对象的父对象，......）
    - :meth:`descendants()` 表示与其直接关联的后代对象（包括子对象、以及子对象的子对象，......）
    - 对于 :meth:`ancestors()` 以及 :meth:`descendants()`：
        - 不包含调用者自身并且返回的列表中没有重复元素
        - 物件顺序是 DFS 顺序

    =====

    Defines the containment relationship of a directed acyclic graph and some practical operations.

    That is, for each object:

    - ``self.parents`` stores the directly associated parent objects.
    - ``self.children`` stores the directly associated child objects.
    - Use :meth:`add()` to establish relationships between objects.
    - Use :meth:`remove()` to cancel relationships between objects.
    - :meth:`ancestors()` represents the ancestor objects directly associated with it
      (including parent objects and parent objects' parent objects, ...).
    - :meth:`descendants()` represents the descendant objects directly associated with it
      (including child objects, and child objects' child objects, ...).
    - For :meth:`ancestors()` and :meth:`descendants()`:
        - Does not include the caller itself, and the returned list has no duplicate elements.
        - The order of the objects is DFS order.
    '''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.parents: list[GRelT] = []
        self.children: list[GRelT] = []

    def mark_refresh(self, func: Callable | str, *, recurse_up=False, recurse_down=False) -> Self:
        super().mark_refresh(func)

        name = func.__name__ if isinstance(func, Callable) else func

        if recurse_up:
            for obj in self.ancestors():
                if hasattr(obj, name):
                    obj.mark_refresh(name)

        if recurse_down:
            for obj in self.descendants():
                if hasattr(obj, name):
                    obj.mark_refresh(name)

    @Signal
    def parents_changed(self) -> None:
        '''
        信号，在 ``self.parents`` 改变时触发

        Signal triggered when ``self.parents`` changes.
        '''
        Relation.parents_changed.emit(self)

    @Signal
    def children_changed(self) -> None:
        '''
        信号，在 ``self.children`` 改变时触发

        Signal triggered when ``self.children`` changes.
        '''
        Relation.children_changed.emit(self)

    def add(self, *objs: RelT) -> Self:
        '''
        向该对象添加子对象

        Add objects to this object.
        '''
        for obj in objs:
            # 理论上这里判断 item not in self.children 就够了，但是防止
            # 有被私自修改 self.parents 以及 self.children 的可能，所以这里都判断了
            # Theoretically, checking item not in self.children is enough here, but to prevent
            # possible modifications to self.parents and self.children, both checks are made here.
            if obj not in self.children:
                self.children.append(obj)
            if self not in obj.parents:
                obj.parents.append(self)

        self.children_changed()
        obj.parents_changed()
        return self

    def remove(self, *objs: RelT) -> Self:
        '''
        从该对象移除子对象

        Remove objects from this object.
        '''
        for obj in objs:
            # 理论上这里判断 `item in self.children` 就够了，原因同 `add`
            # Theoretically, checking `item in self.children` is enough here, for the same reason as `add`.
            try:
                self.children.remove(obj)
            except ValueError: ...
            try:
                obj.parents.remove(self)
            except ValueError: ...

        self.children_changed()
        obj.parents_changed()
        return self

    def _family(self, *, up: bool) -> list[GRelT]:  # use DFS
        lst = self.parents if up else self.children
        res = []

        for sub_obj in lst:
            res.append(sub_obj)
            res.extend(filter(
                lambda obj: obj not in res,
                sub_obj._family(up=up)
            ))

        return res

    @parents_changed.self_refresh_of_relation(recurse_down=True)
    @refresh.register
    def ancestors(self) -> list[GRelT]:
        '''
        获得祖先对象列表

        Get a list of ancestor objects.
        '''
        return self._family(up=True)

    @children_changed.self_refresh_of_relation(recurse_up=True)
    @refresh.register
    def descendants(self) -> list[GRelT]:
        '''
        获得后代对象列表

        Get a list of descendant objects.
        '''
        return self._family(up=False)

    @staticmethod
    def _walk_lst(base_cls: Type[RelT] | None, lst: list[GRelT]) -> Generator[RelT, None, None]:
        if base_cls is None:
            base_cls = Relation

        for obj in lst:
            if isinstance(obj, base_cls):
                yield obj

    def _walk_nearest_family(
        self: Relation,
        base_cls: Type[RelT],
        fn_family: Callable[[Relation], list[Relation]]
    ) -> Generator[RelT, None, None]:

        lst = fn_family(self)[:]

        while lst:
            obj = lst.pop(0)
            if isinstance(obj, base_cls):
                # DFS 结构保证了使用该做法进行剔除的合理性
                # DFS structure ensures the validity of using this method for removal.
                for sub_obj in fn_family(obj):
                    if not lst:
                        break
                    if lst[0] is sub_obj:
                        lst.pop(0)
                yield obj

    def walk_ancestors(self, base_cls: Type[RelT] = None) -> Generator[RelT, None, None]:
        '''
        遍历祖先节点中以 ``base_cls`` （缺省则遍历全部）为基类的对象

        Traverse ancestor nodes with base_cls (default to traverse all) as the base class.
        '''
        yield from self._walk_lst(base_cls, self.ancestors())

    def walk_descendants(self, base_cls: Type[RelT] = None) -> Generator[RelT, None, None]:
        '''
        遍历后代节点中以 ``base_cls`` （缺省则遍历全部）为基类的对象

        Traverse descendant nodes with base_cls (default to traverse all) as the base class.
        '''
        yield from self._walk_lst(base_cls, self.descendants())

    def walk_nearest_ancestors(self, base_cls: Type[RelT]) -> Generator[RelT, None, None]:
        '''
        遍历祖先节点中以 ``base_cls`` 为基类的对象，但是排除已经满足条件的对象的祖先对象

        Traverse ancestor nodes with base_cls as the base class,
        but exclude the ancestors of objects that already meet the conditions.
        '''
        yield from self._walk_nearest_family(base_cls, lambda rel: rel.ancestors())

    def walk_nearest_descendants(self, base_cls: Type[RelT]) -> Generator[RelT, None, None]:
        '''
        遍历后代节点中以 ``base_cls`` 为基类的对象，但是排除已经满足条件的对象的后代对象

        Traverse descendant nodes with base_cls as the base class,
        but exclude the descendants of objects that already meet the conditions.
        '''
        yield from self._walk_nearest_family(base_cls, lambda rel: rel.descendants())
