def trace_signals(signal, *, output=print, suppress=()):
    """Patch a Django signal class or instance to produce output when sent or received.

    Args:
        signal: class or instance to patch
        output: Function to handle output messages
        suppress: names of signals that should produce no output

    Returns:
        A context manager that will do the patch on enter, and try to revert the
        patch on exit; patched signal receivers might survive in caches, although
        an effort is made to keep the Signal.sender_receivers_cache clean.
    """
    from contextlib import contextmanager, wraps
    from typing import Any, Dict, NamedTuple
    from django.dispatch import Signal

    suppress = set(suppress or ())

    sentinel = object()

    class Context:
        """Context manager during signal sending and receiving.

        If thread-safety is every needed, this is the place that needs safening.

        (This is NOT the patching context manager returned by `trace_signals`.)
        """

        base_indent = ' ' * 4

        # the following are not thread-safe:
        depth = 0
        call_count = 0
        current_context = None

        @classmethod
        def with_call(cls, *args, **kwargs):
            Context.call_count += 1
            call = SigCall(Context.call_count, *args, **kwargs)
            return cls(call)

        def __init__(self, call=None):
            self.parent = Context.current_context
            self.call = call or self.parent.call

        def __enter__(self):
            Context.current_context = self
            Context.depth += 1
            return self

        def __exit__(self, *args):
            Context.current_context = self.parent
            Context.depth -= 1

        def log(self, msg):
            call = self.call
            indent = self.base_indent * self.depth
            if call.name not in suppress:
                output(indent + msg)

        def log_call(self):
            call = self.call
            sender = call.sender
            if isinstance(sender, type):
                sender = sender.__name__
                instance = call.kwargs.get('instance')
                if instance and hasattr(instance, 'pk'):
                    sender += f' ({instance.pk})'
            else:
                sender = repr(sender)
            if call.module:
                qualname = f'{call.module}.{call.name}'
            else:
                qualname = call.name
            self.log(f'{call.method} {call.number} {qualname} {sender}')

        def log_receive(self, receiver):
            real_receiver = resolve_receiver(receiver)
            receiver_name = get_receiver_name(real_receiver)
            call = self.call
            self.log(f'RECEIVING {call.number} {call.name}: {receiver_name}')


    class SigCall(NamedTuple):
        number: int
        method: str
        module: str
        name: str
        sender: Any
        kwargs: Dict[str, Any]

    def get_signal_name(signal_instance):
        """Guess appropriate name of signal instance from its referrers.

        Returns:
            Pair `(module_name, attribute_name)` for the signal.
            `module_name` may be empty.
        """
        import gc
        referrers = gc.get_referrers(signal_instance)
        reference_names = [
            (r.get('__name__', ''), name)
            for r in referrers if isinstance(r, dict)
            for name, value in r.items() if value is signal_instance
        ]
        return sorted(
            reference_names,
            key=lambda i: (
                bool(i[0]),  # prefer referer name (e.g. module)
                bool(i[0]) and i[0].startswith('django.'),  # prefer django modules
                i[1].startswith('_'), # prefer non-private names
                len(i[1]),  # prefer longer names
                i,  # alphabetical sort for determinism
            )
        )[-1]

    def resolve_receiver(receiver):
        """"If the registered receiver is a proxy, return the real one instead."""
        func = getattr(receiver, '__func__', None)
        instance = getattr(receiver, '__self__', None)
        if func and func.__name__ == 'on_signal' and instance and hasattr(instance, 'receiver'):
            return instance.receiver  # receiver looks like viewflow StartSignal
        return receiver

    def get_receiver_name(receiver):
        """Work out a diplayable name from a receiver function."""
        if hasattr(receiver, '__qualname__') and hasattr(receiver, '__module__'):
            return f'{receiver.__module__}.{receiver.__qualname__}'
        return repr(receiver)

    def patch_receiver(receiver):
        """Return a wrapper that produces output when receiver is called."""
        is_wrapped = getattr(receiver, '__is_wrapped', None)
        if is_wrapped:
            if is_wrapped is sentinel:  # aldready wrapped by same call
                return receiver
            receiver = receiver.__receiver  # replace old wrapper

        @wraps(receiver)
        def receiver_wrapper(*a, **kw):
            Context.current_context.log_receive(receiver)
            with Context():
                return receiver(*a, **kw)
        receiver_wrapper.__is_wrapped = sentinel
        receiver_wrapper.__receiver = receiver

        return receiver_wrapper

    def patch_signal(signal):
        """Patch signal class or instance to wrap its receivers and produce output when sent."""
        if isinstance(signal, Signal):
            is_instance = True
            signal_cls = type(signal)
        else:
            is_instance = False
            signal_cls = signal
        original_live_receivers = signal_cls._live_receivers
        original_send = signal_cls.send
        original_send_robust = signal_cls.send_robust

        @wraps(original_live_receivers)
        def _live_receivers_wrapper(signal_instance, *a, **kw):
            receivers = original_live_receivers(signal_instance, *a, **kw)
            # signal_instance.sender_receivers_cache.clear()
            return [
                patch_receiver(r)
                for r in receivers
            ]

        @wraps(original_send)
        def send_wrapper(signal_instance, sender, **kw):
            module_name, sig_name = get_signal_name(signal_instance)
            context = Context.with_call('SEND', module_name, sig_name, sender, kw)
            context.log_call()
            with context:
                return original_send(signal_instance, sender, **kw)

        @wraps(original_send_robust)
        def send_robust_wrapper(signal_instance, sender, **kw):
            module_name, sig_name = get_signal_name(signal_instance)
            context = Context.with_call('SEND_ROBUST', module_name, sig_name, sender, kw)
            context.log_call()
            with context:
                return original_send_robust(signal_instance, sender, **kw)

        if is_instance:
            from types import MethodType
            orignal_bound_live_receivers = signal._live_receivers
            original_bound_send = signal.send
            original_bound_send_robust = signal.send_robust

            signal._live_receivers = MethodType(_live_receivers_wrapper, signal)
            signal.send = MethodType(send_wrapper, signal)
            signal.send_robust = MethodType(send_robust_wrapper, signal)

            def unpatch():
                signal._live_receivers = orignal_bound_live_receivers
                signal.send = original_bound_send
                signal.send_robust = original_bound_send_robust

        else:
            signal_cls._live_receivers = _live_receivers_wrapper
            signal_cls.send = send_wrapper
            signal_cls.send_robust = send_robust_wrapper

            def unpatch():
                signal_cls._live_receivers = original_live_receivers
                signal_cls.send = original_send
                signal_cls.send_robust= original_send_robust

        return unpatch

    @contextmanager
    def patched_signal_context():
        unpatch = patch_signal(signal)
        try:
            yield
        finally:
            unpatch()

    return patched_signal_context()
