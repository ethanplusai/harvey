"""Conversation data model."""

import json
from datetime import datetime

from pydantic import BaseModel, Field


class Message(BaseModel):
    sender: str  # "harvey" or "prospect"
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Conversation(BaseModel):
    id: str
    prospect_id: str
    campaign_id: str = ""
    channel: str = "email"
    thread: list[Message] = []
    intent: str = ""  # interested/objection/not_interested/ooo/wrong_person
    status: str = "open"  # open/replied/meeting_booked/closed
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def thread_json(self) -> str:
        return json.dumps([m.model_dump(mode="json") for m in self.thread])

    @classmethod
    def thread_from_json(cls, data: str) -> list[Message]:
        return [Message(**m) for m in json.loads(data)]
