# Client

::: thalovant.client.ThalovantClient

::: thalovant.client.AsyncThalovantClient


Connection deadlines include authenticated readiness as of 0.5.8. A timeout
raises `ThalovantConnectionError`; the additional best-effort readiness allowance
from 0.5.7 is removed. Timed-out and cancelled attempts retain cleanup ownership,
so a later connect waits for that work within its own deadline. Explicit close
uses a caller deadline; `wait_closed()` observes retained cleanup after timeout.
