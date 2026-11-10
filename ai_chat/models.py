from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ChatMessage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lecture_questions")
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="chat_messages")
    question = models.TextField()
    answer = models.TextField()
    sources = models.JSONField(default=list, blank=True)
    transcript_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def clean(self):
        super().clean()
        if self.lecture_id and self.user_id and self.lecture.owner_id != self.user_id:
            raise ValidationError({"lecture": "You may only query your own lectures."})
