from discord_tron_master import __main__

print(f"We are running via {__name__}")
if 'gunicorn_entry' in __name__:
    running = True
    print(f"Running main: {__main__.api.app}")
    api = __main__.api.app