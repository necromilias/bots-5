"""Compositor-native movement and edge resizing for B.O.T.S.-owned windows."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class _NativeWindowInputDispatcher(QObject):
    """One application callback, irrespective of retained desktop windows."""
    def eventFilter(self, watched, event):
        if event.type() not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            return False
        if not isinstance(watched, QWidget):
            return False
        helper = getattr(watched.window(), "_native_edges", None)
        return bool(helper is not None and helper.eventFilter(watched, event))


class NativeWindowEdges(QObject):
    """Hit-test edges only; Qt/compositor owns every resize operation."""
    def __init__(self, window):
        super().__init__(window)
        application = QApplication.instance()
        if application is not None:
            dispatcher = getattr(application, "_bots_native_window_input_dispatcher", None)
            if dispatcher is None:
                dispatcher = _NativeWindowInputDispatcher(application)
                application._bots_native_window_input_dispatcher = dispatcher
                application.installEventFilter(dispatcher)
        window._native_edges = self
        window.setMouseTracking(True)

    def edges_at(self, point):
        window = self.parent()
        if window is None or window.isMaximized() or window.isFullScreen():
            return Qt.Edge(0)
        margin = 6
        edges = Qt.Edge(0)
        horizontal = window.minimumWidth() < window.maximumWidth()
        vertical = window.minimumHeight() < window.maximumHeight()
        if horizontal and point.x() < margin:
            edges |= Qt.Edge.LeftEdge
        elif horizontal and point.x() >= window.width() - margin:
            edges |= Qt.Edge.RightEdge
        if vertical and point.y() < margin:
            edges |= Qt.Edge.TopEdge
        elif vertical and point.y() >= window.height() - margin:
            edges |= Qt.Edge.BottomEdge
        return edges

    def eventFilter(self, watched, event):
        window = self.parent()
        if window is None or not isinstance(watched, QWidget) or watched.window() is not window:
            return False
        kind = event.type()
        if kind not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            return False
        point = window.mapFromGlobal(event.globalPosition().toPoint())
        edges = self.edges_at(point)
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and edges:
            handle = window.windowHandle()
            accepted = bool(handle is not None and handle.startSystemResize(edges))
            window._last_system_chrome_operation = ("resize", edges, accepted)
            if accepted:
                event.accept()
                return True
        if kind == QEvent.Type.MouseMove:
            horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
            vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
            if horizontal and vertical:
                descending = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
                cursor = Qt.CursorShape.SizeFDiagCursor if descending else Qt.CursorShape.SizeBDiagCursor
            elif horizontal:
                cursor = Qt.CursorShape.SizeHorCursor
            elif vertical:
                cursor = Qt.CursorShape.SizeVerCursor
            else:
                cursor = Qt.CursorShape.ArrowCursor
            window.setCursor(cursor)
        return False
