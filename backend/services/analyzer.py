def analyze_resume(resume_text, job_description):
    resume_words = set(resume_text.lower().split())
    job_words = set(job_description.lower().split())

    matched = sorted(list(resume_words.intersection(job_words)))
    missing = sorted(list(job_words - resume_words))

    score = 0
    if len(job_words) > 0:
        score = round((len(matched) / len(job_words)) * 100)

    return {
        "match_score": score,
        "matched_words": matched[:30],
        "missing_words": missing[:30]
    }
