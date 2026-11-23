import unittest

from email_collector.job_extractor import (
    _has_malformed_job_fields,
    extract_jobs_from_email,
)


class ExtractJobsFromEmailTests(unittest.TestCase):
    def test_ignores_linkedin_badges_when_extracting_job_fields(self):
        body = """Your job alert for QA

Senior MLOps Engineer
adesso SE
Hannover
This company is actively hiring
View job: https://www.linkedin.com/comm/jobs/view/4391861801/?tracking=x
"""

        jobs = extract_jobs_from_email(body)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            (jobs[0]["title"], jobs[0]["company"], jobs[0]["location"]),
            ("Senior MLOps Engineer", "adesso SE", "Hannover"),
        )

    def test_keeps_each_listing_separate_in_multi_job_alert(self):
        body = """First QA title
Company One
Berlin
View job: https://www.linkedin.com/jobs/view/1111111111/
----------
Second QA title
Company Two
Remote
Apply with resume & profile
View job: https://www.linkedin.com/jobs/view/2222222222/
"""

        jobs = extract_jobs_from_email(body)

        self.assertEqual(
            [
                (job["title"], job["company"], job["location"])
                for job in jobs
            ],
            [
                ("First QA title", "Company One", "Berlin"),
                ("Second QA title", "Company Two", "Remote"),
            ],
        )

    def test_does_not_treat_missing_location_as_metadata(self):
        self.assertFalse(
            _has_malformed_job_fields("QA Engineer", "Company", "")
        )

    def test_detects_badges_saved_as_job_fields(self):
        self.assertTrue(
            _has_malformed_job_fields(
                "QA Engineer", "Company", "Apply with resume & profile"
            )
        )


if __name__ == "__main__":
    unittest.main()
