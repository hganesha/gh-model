import asyncio

from payments.ports.gateway import Notifier


class EmailNotifier(Notifier):
    async def notify(self, customer_id: int, text: str) -> None:
        await asyncio.sleep(0)


async def notify_all(notifier: Notifier, customer_ids: list[int], text: str) -> None:
    await asyncio.gather(*[notifier.notify(c, text) for c in customer_ids])
