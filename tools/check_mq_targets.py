"""Compatibility entry point for the 05 audit/regression work package."""

from work_packages.audit_regression_05.check_mq_targets import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
