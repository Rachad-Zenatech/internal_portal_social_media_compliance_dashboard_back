"""Unread media recorded in a submission's saved compliance check."""


def failed_reads(video, images, documents):
    failures = []
    if video and video["transcript_status"] == "FAILED":
        failures.append({"what": "video transcript", "name": video.get("original_name"), "error": video.get("transcript_error")})
    failures.extend({"what": "image text", "name": image.get("original_name"), "error": image.get("image_text_error")}
                    for image in images if image["image_text_status"] == "FAILED")
    failures.extend({"what": "document", "name": doc.get("original_name"), "error": doc.get("doc_text_error")}
                    for doc in documents if doc["doc_text_status"] == "FAILED")
    return failures
