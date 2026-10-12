# Recovery

Source: `<package>/recovery.py`. If the harness stops mid-wake, the next start closes
that wake: calls that never started are marked not run; read-only ones that were cut off
are safe to repeat; anything else is "uncertain" (check before repeating). Nothing is
retried automatically. You get a recovery note and, within limits, a recovery wake.
Shell sessions that were running are reported as ended.
