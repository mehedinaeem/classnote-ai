from django import forms
from django.conf import settings


class StudyGenerationForm(forms.Form):
    question_count = forms.IntegerField(min_value=1, max_value=20, label="MCQs")
    flashcard_count = forms.IntegerField(min_value=1, max_value=20, label="Flashcards")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["question_count"].initial = settings.QUIZ_QUESTION_COUNT
        self.fields["flashcard_count"].initial = settings.FLASHCARD_COUNT
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
