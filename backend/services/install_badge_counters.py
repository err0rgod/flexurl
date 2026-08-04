import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.append(PROJECT_DIR)

load_dotenv(os.path.join(PROJECT_DIR, ".env"))


INSTALL_SQL = """
CREATE TABLE IF NOT EXISTS public.badge_counters (
    name TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0 CHECK (value >= 0),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

INSERT INTO public.badge_counters (name, value)
VALUES
    ('links_created', GREATEST(435, COALESCE((SELECT COUNT(*) FROM public.urldata), 0))),
    ('visitors', GREATEST(113, COALESCE((SELECT COUNT(*) FROM public.clicklog), 0))),
    ('redirects', GREATEST(1789, COALESCE((SELECT COUNT(*) FROM public.clicklog), 0)))
ON CONFLICT (name) DO UPDATE
SET
    value = GREATEST(public.badge_counters.value, EXCLUDED.value),
    updated_at = NOW();

CREATE OR REPLACE FUNCTION public.increment_badge_counter(counter_name TEXT)
RETURNS VOID AS $$
BEGIN
    INSERT INTO public.badge_counters (name, value, updated_at)
    VALUES (counter_name, 1, NOW())
    ON CONFLICT (name) DO UPDATE
    SET value = public.badge_counters.value + 1,
        updated_at = NOW();
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.count_created_link_for_badge()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM public.increment_badge_counter('links_created');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.count_visitor_for_badge()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM public.increment_badge_counter('visitors');
    PERFORM public.increment_badge_counter('redirects');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'badge_count_created_link'
    ) THEN
        CREATE TRIGGER badge_count_created_link
        AFTER INSERT ON public.urldata
        FOR EACH ROW
        EXECUTE FUNCTION public.count_created_link_for_badge();
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'badge_count_visitor'
    ) THEN
        CREATE TRIGGER badge_count_visitor
        AFTER INSERT ON public.clicklog
        FOR EACH ROW
        EXECUTE FUNCTION public.count_visitor_for_badge();
    END IF;
END $$;
"""


def main() -> int:
    db_path = os.getenv("DB_PATH")
    if not db_path:
        print("DB_PATH is not set.", file=sys.stderr)
        return 1

    engine = create_engine(db_path, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text(INSTALL_SQL))
        rows = connection.execute(
            text("SELECT name, value FROM public.badge_counters ORDER BY name")
        ).all()

    print("Badge counters installed.")
    for name, value in rows:
        print(f"{name}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
