from .main import main
import asyncio

is_restart = False
while True:
    restart = asyncio.run(main(is_restart=is_restart))
    if not restart:
        break
    is_restart = True
