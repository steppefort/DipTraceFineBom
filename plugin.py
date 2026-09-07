"""DipTraceSchPluginAdapter entry point for FineBOM."""
from finebom.workflow import run


def main(ctx):
    run(ctx)
    return 0
