import asyncio

from .app import main

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nБот остановлен пользователем.")
