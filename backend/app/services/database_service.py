"""
Database operations service using the Supabase Python client (HTTPS/REST).

Replaces direct asyncpg connections which require the Supabase IPv4 add-on.
The Supabase client communicates over HTTPS (port 443) — works on all plans.
Public interface is identical to the previous implementation.
"""
import asyncio
from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import datetime

from supabase import create_client, Client

from app.config import settings
from app.models.entities import UserProfile, Resume, Analysis, AnalysisResult
from app.utils.logger import get_logger
from app.utils.async_utils import async_timer
from app.core.exceptions import DatabaseError

logger = get_logger(__name__)


def _get_supabase() -> Client:
    """Return a Supabase client using the service-role key (bypasses RLS for server-side ops)."""
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _run(fn):
    """Run a synchronous supabase-py call in a thread so we don't block the event loop."""
    return await asyncio.to_thread(fn)


# ---------------------------------------------------------------------------
# Stub connection manager — kept so existing callers (main.py) still work
# ---------------------------------------------------------------------------

class DatabaseConnectionManager:
    """Thin compatibility shim — real I/O is done via the Supabase REST client."""

    async def initialize(self) -> None:
        """Verify connectivity by pinging the Supabase REST endpoint."""
        try:
            client = _get_supabase()
            # A lightweight table probe — succeeds as long as the project is reachable
            await _run(lambda: client.table("resumes").select("id").limit(1).execute())
            logger.info("Supabase REST client initialised successfully",
                        url=settings.SUPABASE_URL)
        except Exception as e:
            # Non-fatal: log and continue; individual operations will surface errors
            logger.warning("Supabase connectivity check failed — will retry on first use",
                           error=str(e))

    async def close(self) -> None:
        """No persistent connections to close."""
        logger.info("Database service closed (no-op for Supabase REST client)")

    async def health_check(self) -> Dict[str, Any]:
        try:
            client = _get_supabase()
            await _run(lambda: client.table("resumes").select("id").limit(1).execute())
            return {"status": "healthy", "backend": "supabase-rest", "timestamp": datetime.utcnow().isoformat()}
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return {"status": "unhealthy", "error": str(e), "timestamp": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

class UserRepository:
    """Repository for user-profile operations."""

    def __init__(self, connection_manager: DatabaseConnectionManager):
        self.connection_manager = connection_manager  # kept for interface compat

    @async_timer
    async def get_user_by_id(self, user_id: UUID) -> Optional[UserProfile]:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("profiles")
                    .select("id, email, full_name, avatar_url, created_at")
                    .eq("id", str(user_id))
                    .single()
                    .execute()
            )
            if res.data:
                d = res.data
                return UserProfile(
                    id=d["id"],
                    email=d["email"],
                    full_name=d.get("full_name"),
                    avatar_url=d.get("avatar_url"),
                    created_at=d.get("created_at"),
                )
            return None
        except Exception as e:
            logger.error("Failed to get user by ID", user_id=str(user_id), error=str(e))
            raise DatabaseError(f"Failed to get user: {e}")

    @async_timer
    async def create_user_profile(self, user_profile: UserProfile) -> UserProfile:
        try:
            client = _get_supabase()
            await _run(
                lambda: client.table("profiles").upsert({
                    "id": str(user_profile.id),
                    "email": user_profile.email,
                    "full_name": user_profile.full_name,
                    "avatar_url": user_profile.avatar_url,
                }).execute()
            )
            logger.info("User profile upserted", user_id=str(user_profile.id))
            return user_profile
        except Exception as e:
            logger.error("Failed to create user profile", user_id=str(user_profile.id), error=str(e))
            raise DatabaseError(f"Failed to create user profile: {e}")


class ResumeRepository:
    """Repository for resume operations."""

    def __init__(self, connection_manager: DatabaseConnectionManager):
        self.connection_manager = connection_manager

    @async_timer
    async def create_resume(self, resume: Resume) -> Resume:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("resumes").insert({
                    "user_id": str(resume.user_id),
                    "file_name": resume.file_name,
                    "file_url": resume.file_url,
                    "parsed_text": resume.parsed_text,
                }).execute()
            )
            if res.data:
                row = res.data[0]
                resume.id = row.get("id", resume.id)
                resume.uploaded_at = row.get("uploaded_at", resume.uploaded_at)
            logger.info("Resume created", resume_id=str(resume.id), user_id=str(resume.user_id))
            return resume
        except Exception as e:
            logger.error("Failed to create resume", user_id=str(resume.user_id), error=str(e))
            raise DatabaseError(f"Failed to create resume: {e}")

    async def get_resume_by_id(self, resume_id: UUID) -> Optional[Resume]:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("resumes")
                    .select("id, user_id, file_name, file_url, parsed_text, uploaded_at")
                    .eq("id", str(resume_id))
                    .single()
                    .execute()
            )
            if res.data:
                d = res.data
                return Resume(
                    id=d["id"], user_id=d["user_id"], file_name=d["file_name"],
                    file_url=d.get("file_url"), parsed_text=d.get("parsed_text"),
                    uploaded_at=d.get("uploaded_at"),
                )
            return None
        except Exception as e:
            logger.error("Failed to get resume by ID", resume_id=str(resume_id), error=str(e))
            raise DatabaseError(f"Failed to get resume: {e}")

    async def get_user_resumes(self, user_id: UUID) -> List[Resume]:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("resumes")
                    .select("id, user_id, file_name, file_url, parsed_text, uploaded_at")
                    .eq("user_id", str(user_id))
                    .order("uploaded_at", desc=True)
                    .execute()
            )
            return [
                Resume(
                    id=d["id"], user_id=d["user_id"], file_name=d["file_name"],
                    file_url=d.get("file_url"), parsed_text=d.get("parsed_text"),
                    uploaded_at=d.get("uploaded_at"),
                )
                for d in (res.data or [])
            ]
        except Exception as e:
            logger.error("Failed to get user resumes", user_id=str(user_id), error=str(e))
            raise DatabaseError(f"Failed to get user resumes: {e}")

    async def update_resume_text(self, resume_id: UUID, parsed_text: str) -> None:
        try:
            client = _get_supabase()
            await _run(
                lambda: client.table("resumes")
                    .update({"parsed_text": parsed_text})
                    .eq("id", str(resume_id))
                    .execute()
            )
            logger.info("Resume text updated", resume_id=str(resume_id))
        except Exception as e:
            logger.error("Failed to update resume text", resume_id=str(resume_id), error=str(e))
            raise DatabaseError(f"Failed to update resume text: {e}")


class AnalysisRepository:
    """Repository for analysis operations."""

    def __init__(self, connection_manager: DatabaseConnectionManager):
        self.connection_manager = connection_manager

    @async_timer
    async def create_analysis(self, analysis_result: AnalysisResult) -> str:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("analyses").insert({
                    "user_id": str(analysis_result.user_id),
                    "resume_id": str(analysis_result.resume_id),
                    "job_title": analysis_result.job_title,
                    "job_description": analysis_result.job_description,
                    "match_score": round(analysis_result.match_score),
                    "ai_feedback": analysis_result.ai_feedback,
                    "matched_keywords": analysis_result.matched_keywords,
                    "missing_keywords": analysis_result.missing_keywords,
                }).execute()
            )
            if not res.data:
                raise DatabaseError("Analysis insert returned no data")
            analysis_id = str(res.data[0]["id"])
            logger.info("Analysis created", analysis_id=analysis_id,
                        user_id=str(analysis_result.user_id),
                        match_score=analysis_result.match_score)
            return analysis_id
        except Exception as e:
            logger.error("Failed to create analysis", user_id=str(analysis_result.user_id), error=str(e))
            raise DatabaseError(f"Failed to create analysis: {e}")

    async def get_analysis_by_id(self, analysis_id: UUID) -> Optional[Analysis]:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("analyses")
                    .select("id, user_id, resume_id, job_title, job_description, "
                            "match_score, ai_feedback, matched_keywords, missing_keywords, created_at")
                    .eq("id", str(analysis_id))
                    .single()
                    .execute()
            )
            if res.data:
                d = res.data
                return Analysis(
                    id=d["id"], user_id=d["user_id"], resume_id=d.get("resume_id"),
                    job_title=d.get("job_title"), job_description=d.get("job_description"),
                    match_score=d.get("match_score"), ai_feedback=d.get("ai_feedback"),
                    matched_keywords=d.get("matched_keywords"),
                    missing_keywords=d.get("missing_keywords"),
                    created_at=d.get("created_at"),
                )
            return None
        except Exception as e:
            logger.error("Failed to get analysis by ID", analysis_id=str(analysis_id), error=str(e))
            raise DatabaseError(f"Failed to get analysis: {e}")

    async def get_user_analyses(self, user_id: UUID, limit: int = 50, offset: int = 0) -> List[Analysis]:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("analyses")
                    .select("id, user_id, resume_id, job_title, job_description, "
                            "match_score, ai_feedback, matched_keywords, missing_keywords, created_at")
                    .eq("user_id", str(user_id))
                    .order("created_at", desc=True)
                    .range(offset, offset + limit - 1)
                    .execute()
            )
            return [
                Analysis(
                    id=d["id"], user_id=d["user_id"], resume_id=d.get("resume_id"),
                    job_title=d.get("job_title"), job_description=d.get("job_description"),
                    match_score=d.get("match_score"), ai_feedback=d.get("ai_feedback"),
                    matched_keywords=d.get("matched_keywords"),
                    missing_keywords=d.get("missing_keywords"),
                    created_at=d.get("created_at"),
                )
                for d in (res.data or [])
            ]
        except Exception as e:
            logger.error("Failed to get user analyses", user_id=str(user_id), error=str(e))
            raise DatabaseError(f"Failed to get user analyses: {e}")

    async def get_user_analyses_count(self, user_id: UUID) -> int:
        try:
            client = _get_supabase()
            res = await _run(
                lambda: client.table("analyses")
                    .select("id", count="exact")
                    .eq("user_id", str(user_id))
                    .execute()
            )
            return res.count or 0
        except Exception as e:
            logger.error("Failed to get user analyses count", user_id=str(user_id), error=str(e))
            raise DatabaseError(f"Failed to get user analyses count: {e}")


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class DatabaseService:
    """Main database service orchestrating all repositories."""

    def __init__(self):
        self.connection_manager = DatabaseConnectionManager()
        self.users = UserRepository(self.connection_manager)
        self.resumes = ResumeRepository(self.connection_manager)
        self.analyses = AnalysisRepository(self.connection_manager)

    async def initialize(self) -> None:
        await self.connection_manager.initialize()
        logger.info("Database service initialized (Supabase REST)")

    async def close(self) -> None:
        await self.connection_manager.close()
        logger.info("Database service closed")

    async def health_check(self) -> Dict[str, Any]:
        return await self.connection_manager.health_check()

    async def store_analysis(self, analysis_result: AnalysisResult) -> str:
        return await self.analyses.create_analysis(analysis_result)

    async def get_user_analyses(self, user_id: UUID, limit: int = 50, offset: int = 0) -> List[Analysis]:
        return await self.analyses.get_user_analyses(user_id, limit, offset)

    async def get_analysis_by_id(self, analysis_id: UUID) -> Optional[Analysis]:
        return await self.analyses.get_analysis_by_id(analysis_id)


# Global instance
db_service = DatabaseService()