from django import forms

from .models import Course


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = [
            "course_code", "course_name", "teacher_name",
            "department", "semester", "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        if self.is_bound:
            for name in self.errors:
                if name in self.fields:
                    self.fields[name].widget.attrs.update({
                        "class": "form-control is-invalid", "aria-invalid": "true",
                    })
