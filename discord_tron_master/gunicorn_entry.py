from discord_tron_master import __main__

application = __main__.api.app
running = False
print(f"We are running via {__name__}")
if 'gunicorn_entry' in __name__ and running is False:
    running = True
    print(f"Running main.")
    __main__.main()
