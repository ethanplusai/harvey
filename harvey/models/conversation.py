"""Conversation data model."""

import json
from datetime import datetime

from pydantic import BaseModel, Field


class Message(BaseModel):
    sender: str  # "harvey" or "prospect"
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# Sales stages: tracks where the deal is in the pipeline
STAGES = [
    "initial_outreach",  # first emails sent, no reply yet
    "engaged",           # prospect replied positively
    "qualifying",        # assessing fit (budget, authority, need, timing)
    "presenting",        # sharing product details, case studies
    "negotiating",       # discussing terms, pricing, objections
    "closing",           # moving toward a meeting or deal
    "closed_won",        # meeting booked or deal closed
    "closed_lost",       # prospect said no or went dark
]


class Conversation(BaseModel):
    id: str
    prospect_id: str
    campaign_id: str = ""
    channel: str = "email"
    thread: list[Message] = []
    intent: str = ""  # interested/objection/not_interested/ooo/wrong_person
    stage: str = "initial_outreach"  # sales pipeline stage
    status: str = "open"  # open/replied/meeting_booked/closed
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def thread_json(self) -> str:
        return json.dumps([m.model_dump(mode="json") for m in self.thread])

    @classmethod
    def thread_from_json(cls, data: str) -> list[Message]:
        return [Message(**m) for m in json.loads(data)]
