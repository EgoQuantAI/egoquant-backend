-- EgoQuant accounts: profile picture. Run in the Supabase SQL editor after 001, 002_hardening and 002_what_if.
-- Idempotent. Adds one nullable column; nothing is renamed or removed.
--
-- profiles.avatar is either a built-in picture ("preset:p1".."preset:p5", drawn by the frontend) or the user's own
-- small raster image as a data URL (JPEG/PNG/WebP, resized to 160 px in the browser). The backend checks the bytes
-- really are that format (egoquant/accounts/routes.py, valid_avatar); this constraint is the database's own lock.
-- SVG is never accepted: it can carry script. Deleting the account deletes the picture with the profile row.

alter table public.profiles add column if not exists avatar text;

alter table public.profiles drop constraint if exists profiles_avatar_check;
alter table public.profiles add constraint profiles_avatar_check check (
    avatar is null
    or avatar ~ '^preset:p[1-5]$'
    or (avatar ~ '^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$' and length(avatar) <= 90000)
);

-- The user may change their username and picture; user_id and created_at stay fixed (002_hardening).
grant update (username, avatar) on public.profiles to authenticated;
