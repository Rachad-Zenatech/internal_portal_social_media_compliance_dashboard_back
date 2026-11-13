"""The "Text in image 1:" labels the check puts before each image's text, and keeping them out of the wording match."""

import re

IMAGE_MARK = re.compile(r"Text in image \d+\s*:", re.IGNORECASE)


def without_image_marks(text: str) -> str:
    """The text without those labels. With the label's punctuation gone, "image 1: DRONE ..." would read as the words
    "1 drone" and trip a rule's term."""
    return IMAGE_MARK.sub("\n", text)
