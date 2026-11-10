from django import forms


class QuestionForm(forms.Form):
    question = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(attrs={
            "class": "form-control", "rows": 3, "placeholder": "What did the teacher say about kernel trick?",
        }),
    )
