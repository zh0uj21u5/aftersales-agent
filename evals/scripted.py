"""A deterministic user that never reads gold actions or grading results."""

from evals.models import EvalTask

STOP = "###STOP###"


class ScriptedUser:
    def __init__(self, task: EvalTask) -> None:
        self.opening = task.opening_message
        self.replies = task.scripted_replies
        self.index = -1

    def next_message(self) -> str | None:
        self.index += 1
        text = (
            self.opening
            if self.index == 0
            else self.replies[min(self.index - 1, len(self.replies) - 1)]
        )
        return None if text == STOP else text
