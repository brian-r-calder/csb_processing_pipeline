import sys
import click
import tkinter as tk
from ocscsb import __version__ as version

@click.command()
@click.version_option(version=version)
def cli() -> None:
    root = tk.Tk()
    tk.mainloop()

if getattr(sys, 'frozen', False):
    cli(sys.argv[1:])