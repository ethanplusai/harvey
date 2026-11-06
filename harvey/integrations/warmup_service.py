"""Which warmup service Harvey reads: TrulyInbox or InboxWarm.

TrulyInbox is the one Harvey recommends. InboxWarm works too for anyone on it;
when both keys are set, TrulyInbox wins.
"""

from harvey.integrations import inboxwarm as iw
from harvey.integrations import trulyinbox as ti


def active() -> str:
    """'trulyinbox', 'inboxwarm', or '' when no warmup key is set."""
    if ti.key_from_env()[0]:
        return "trulyinbox"
    if iw.key_from_env():
        return "inboxwarm"
    return ""


def label(service: str | None = None) -> str:
    service = active() if service is None else service
    return {"inboxwarm": iw.NAME}.get(service, "TrulyInbox")


async def current_health(address: str):
    """(health, error, label) from whichever service is connected."""
    service = active()
    if service == "inboxwarm":
        health, error = await iw.current_health(address)
    else:
        health, error = await ti.current_health(address)
    return health, error, label(service)
