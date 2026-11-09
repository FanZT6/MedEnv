# Modified for MedEnv; see NOTICE.
import warnings

import click


def colorful_print(string: str, *args, **kwargs) -> None:
    end = kwargs.pop("end", "\n")
    print(click.style(string, *args, **kwargs), end=end, flush=True)

def colorful_warning(string: str, *args, **kwargs) -> None:
    warnings.warn(click.style(string, *args, **kwargs), stacklevel=2)
