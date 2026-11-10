from django import template
from transcription.utils import format_timestamp

register = template.Library()
register.filter("timestamp", format_timestamp)
