from django import forms

from courses.models import Course
from .models import Lecture
from .services import inspect_audio
from .validators import validate_audio_upload


class LectureForm(forms.ModelForm):
    audio_file = forms.FileField(
        validators=[validate_audio_upload],
        widget=forms.ClearableFileInput(attrs={"accept": ".mp3,.wav,.m4a,.mp4,.webm,.ogg"}),
        help_text="MP3, WAV, M4A, MP4, WebM, or OGG. The file must contain audio.",
    )

    class Meta:
        model = Lecture
        fields = ["course", "title", "lecture_date", "description", "audio_file"]
        widgets = {
            "lecture_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.owner = user
        self.fields["course"].queryset = Course.objects.filter(owner=user)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name == "course" else "form-control"

    def clean_audio_file(self):
        upload = self.cleaned_data["audio_file"]
        inspect_audio(upload)
        return upload


class RecordingForm(LectureForm):
    class Meta(LectureForm.Meta):
        fields = ["course", "title", "audio_file"]
