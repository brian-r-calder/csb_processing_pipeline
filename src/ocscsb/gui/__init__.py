import sys
import click
import tkinter as tk
from ocscsb import __version__ as version
from ocscsb.gui.components import MainWindow

@click.command()
@click.version_option(version=version)
def cli() -> None:
    root = tk.Tk()
    win = MainWindow(root)
    tk.mainloop()
    sys.exit(0)

if getattr(sys, 'frozen', False):
    cli(sys.argv[1:])