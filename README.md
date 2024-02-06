# async-typer

`async-typer` is a _simple async wrapper_ for the [typer](https://github.com/tiangolo/typer) library. 
We already have a lot of async implementations for our applications, but we can't use them easily with typer.
And `async-typer` have more features than typer to solve our real-world problems in a more elegant way.

## Installation

```bash
pip install async-typer
```

## How to use

```python
from async_typer import AsyncTyper


app = AsyncTyper()

@app.command()
def foo():
    service.work()

@app.async_command()
async def bar():
    await service.work_async()

```


## FastAPI-like event handlers

Handle startup and shutdown events with async or sync functions.

```python
app.add_event_handler("startup", redis_async_pool_manager.init_redis_pool)
app.add_event_handler("shutdown", redis_async_pool_manager.close_redis_pool)
```

Please check the [typer documentation](https://typer.tiangolo.com/) for more information.
