from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class BootstrapFormMixin:
    """Apply Bootstrap styling while retaining Django's validation and widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        if self.is_bound:
            for name in self.errors:
                if name in self.fields:
                    self.fields[name].widget.attrs["class"] += " is-invalid"
                    self.fields[name].widget.attrs["aria-invalid"] = "true"


class RegistrationForm(BootstrapFormMixin, UserCreationForm):
    pass


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    pass
