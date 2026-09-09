# Client

::: thalovant.client.ThalovantClient

::: thalovant.client.AsyncThalovantClient


Connection deadlines include authenticated readiness as of 0.5.8. A timeout
raises `ThalovantConnectionError`; the additional best-effort readiness allowance
from 0.5.7 is removed. Timed-out and cancelled attempts retain cleanup ownership,
so a later connect waits for that work within its own deadline. Explicit close
uses a caller deadline; `wait_closed()` observes retained cleanup after timeout.

For `connect()` and `close()`, only `timeout=None` selects the configured default.
Zero, negative, infinite and NaN timeouts expire immediately before transport I/O
or a lifecycle change. Pass a positive finite budget to initiate cleanup.
