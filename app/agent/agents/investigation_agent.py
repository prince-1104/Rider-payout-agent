"""Plan a bounded evidence path; application code supplies every tool argument."""

from app.agent.agents.base import StructuredAgent
from app.agent.state import EvidencePath, InvestigationJob, InvestigationPlan, InvestigationResult
from app.domain.dispute_types import INCENTIVE_TYPES, PENALTY_TYPES, DisputeType
from app.domain.policy import is_dispute_within_window
from app.agent.security import claim_data, guard_untrusted_data


def evidence_jobs(claims, today=None):
    jobs = []
    for index, claim in enumerate(claims):
        if claim.dispute_type == DisputeType.UNKNOWN or (claim.trip_id is None and claim.dispute_date is None):
            continue
        if today is not None and claim.dispute_date is not None and not is_dispute_within_window(claim.dispute_date, today):
            continue
        if claim.dispute_type in INCENTIVE_TYPES:
            if claim.trip_id is not None or claim.dispute_date is None:
                continue
            path = EvidencePath.INCENTIVE
        elif claim.trip_id:
            path = EvidencePath.TRIP
        elif claim.dispute_type in PENALTY_TYPES:
            path = EvidencePath.PENALTY
        else:
            path = EvidencePath.DAY
        jobs.append(InvestigationJob(claim_index=index, path=path))
    return jobs


class InvestigationAgent(StructuredAgent):
    def __init__(self, model):
        super().__init__(model, "investigation.txt", InvestigationPlan, ("claims", "allowed_jobs"))

    def investigate(self, claims, tools, today):
        expected = evidence_jobs(claims, today)
        plan = self.invoke({"claims": [claim_data(c) for c in claims],
                            "allowed_jobs": [j.model_dump(mode="json") for j in expected]})
        if plan.jobs != expected:
            raise ValueError("Investigation must follow the complete validated evidence plan")
        results = []
        for job in expected:
            claim = claims[job.claim_index]
            evidence = {}

            def read(name, arguments):
                result = tools[name].invoke(arguments)
                guard_untrusted_data(result)
                evidence[name] = result
                return evidence[name]

            if job.path == EvidencePath.TRIP:
                trip = read("get_trip", {"trip_id": claim.trip_id})
                read("get_rider_payouts", {"date": trip["ist_date"]})
                read("calculate_trip_expected_payout", {"trip_id": claim.trip_id})
                read("reconcile_trip_payout", {"trip_id": claim.trip_id})
            else:
                arguments = {"date": claim.dispute_date.isoformat()}
                read("find_recent_trips", arguments)
                read("get_rider_payouts", arguments)
                if job.path in {EvidencePath.INCENTIVE, EvidencePath.DAY}:
                    read("calculate_daily_incentive", arguments)
                    read("reconcile_daily_incentive", arguments)
                if job.path in {EvidencePath.PENALTY, EvidencePath.DAY}:
                    read("reconcile_cancellation_penalties", arguments)
            results.append(InvestigationResult(dispute_id=claim.dispute_id, evidence=evidence))
        return results
