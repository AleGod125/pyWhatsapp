-- ===========================================================================
-- whatsapp_backup - ESQUEMA COMPLETO DE LA BASE DE DATOS
-- ===========================================================================
--
-- Motor      : PostgreSQL 18.6  (requiere 13+ por gen_random_uuid())
-- Revision   : b2c3d4e5f6a7   <- la cabeza de Alembic
-- Generado   : 2026-09-08, con `pg_dump --schema-only` sobre la base real.
--
-- QUE ES ESTO
-- -----------
-- El esquema entero en un solo archivo, autocontenido y en el orden correcto:
-- extension -> secuencias -> tablas -> restricciones -> indices -> semilla.
-- Sirve para levantar una base desde cero sin ejecutar las migraciones.
--
-- NO LLEVA NI UNA FILA DE DATOS. Solo estructura, mas la unica fila que hace
-- falta: la revision de Alembic (al final).
--
-- QUIEN MANDA
-- -----------
-- La fuente de verdad siguen siendo las migraciones de `backend/migrations/`.
-- Este archivo es un ATAJO para instalar de cero, no un sustituto: si tocas el
-- esquema, hazlo con una migracion de Alembic y despues regenera esto con
--
--     py backend/tools/exportar_esquema.py
--
-- COMO USARLO
-- -----------
--     createdb whatsapp_backup
--     psql -d whatsapp_backup -f database/schema.sql
--
-- AVISO
-- -----
-- Solo tiene `CREATE`: sobre una base que ya tenga estas tablas fallara con
-- "already exists". Es a proposito -- que falle es mejor que que pise datos.
-- ===========================================================================

--
-- PostgreSQL database dump
--


-- Dumped from database version 18.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: app_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_state (
    key character varying(128) NOT NULL,
    value jsonb,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_history_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_history_state (
    id bigint NOT NULL,
    chat_id bigint NOT NULL,
    chat_jid character varying(128) NOT NULL,
    oldest_message_id character varying(128),
    oldest_message_timestamp bigint,
    newest_message_timestamp bigint,
    message_count integer NOT NULL,
    requests_sent integer NOT NULL,
    responses_received integer NOT NULL,
    history_status character varying(20) NOT NULL,
    last_response_count integer,
    consecutive_no_progress integer NOT NULL,
    last_error text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seed_attempt_at timestamp with time zone,
    last_seed_attempt_result character varying(32),
    oldest_from_me boolean DEFAULT false NOT NULL,
    cursor_source character varying(16),
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    next_retry_at timestamp with time zone,
    CONSTRAINT ck_history_state_status CHECK (((history_status)::text = ANY ((ARRAY['pending'::character varying, 'fetching'::character varying, 'exhausted'::character varying, 'server_limited'::character varying, 'timeout'::character varying, 'error'::character varying, 'no_valid_cursor'::character varying, 'waiting_seed'::character varying, 'empty_confirmed'::character varying])::text[])))
);


--
-- Name: chat_history_state_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_history_state_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_history_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_history_state_id_seq OWNED BY public.chat_history_state.id;


--
-- Name: chats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chats (
    id bigint NOT NULL,
    jid character varying(128) NOT NULL,
    name text,
    chat_type character varying(16) NOT NULL,
    last_message text,
    last_message_timestamp bigint,
    raw_metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    whatsapp_account_id uuid NOT NULL,
    CONSTRAINT ck_chats_chat_type CHECK (((chat_type)::text = ANY ((ARRAY['individual'::character varying, 'group'::character varying, 'broadcast'::character varying, 'newsletter'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: chats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chats_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chats_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chats_id_seq OWNED BY public.chats.id;


--
-- Name: contacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contacts (
    id bigint NOT NULL,
    jid character varying(128) NOT NULL,
    lid character varying(128),
    phone_number character varying(32),
    display_name text,
    push_name text,
    business_name text,
    raw_metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    whatsapp_account_id uuid NOT NULL
);


--
-- Name: contacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.contacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: contacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.contacts_id_seq OWNED BY public.contacts.id;


--
-- Name: drive_folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.drive_folders (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    path character varying(512) NOT NULL,
    folder_id character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: google_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.google_credentials (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    google_subject character varying(255) NOT NULL,
    scope text NOT NULL,
    access_token_encrypted bytea,
    refresh_token_encrypted bytea,
    access_token_expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: google_drive_storage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.google_drive_storage (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    root_folder_id character varying(128) NOT NULL,
    manifest_file_id character varying(128),
    bytes_uploaded bigint NOT NULL,
    files_uploaded integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_verified_at timestamp with time zone,
    last_upload_at timestamp with time zone
);


--
-- Name: history_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.history_requests (
    id bigint NOT NULL,
    protocol_request_id character varying(128),
    peer_data_request_session_id character varying(128),
    chat_id bigint,
    chat_jid character varying(128) NOT NULL,
    cursor_message_id character varying(128),
    cursor_timestamp bigint,
    requested_count integer NOT NULL,
    status character varying(20) NOT NULL,
    sent_at timestamp with time zone DEFAULT now() NOT NULL,
    received_at timestamp with time zone,
    response_count integer,
    error text,
    CONSTRAINT ck_history_requests_status CHECK (((status)::text = ANY ((ARRAY['sent'::character varying, 'acked'::character varying, 'received'::character varying, 'timeout'::character varying, 'error'::character varying])::text[])))
);


--
-- Name: history_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.history_requests_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: history_requests_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.history_requests_id_seq OWNED BY public.history_requests.id;


--
-- Name: history_seeds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.history_seeds (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    whatsapp_account_id uuid NOT NULL,
    chat_id bigint NOT NULL,
    chat_jid character varying(128) NOT NULL,
    wa_msg_id character varying(128) NOT NULL,
    "timestamp" bigint NOT NULL,
    from_me boolean NOT NULL,
    source character varying(24) NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    valid boolean NOT NULL
);


--
-- Name: media_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.media_files (
    id bigint NOT NULL,
    message_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    whatsapp_message_id character varying(128),
    media_type character varying(20) NOT NULL,
    mime_type character varying(255),
    file_name text,
    file_size bigint,
    duration_seconds integer,
    width integer,
    height integer,
    media_url text,
    direct_path text,
    media_key bytea,
    file_sha256 bytea,
    file_enc_sha256 bytea,
    local_path text,
    download_status character varying(20) NOT NULL,
    download_attempts integer NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    drive_file_id character varying(128),
    storage_status character varying(16) NOT NULL,
    stored_bytes bigint,
    uploaded_at timestamp with time zone,
    last_accessed_at timestamp with time zone,
    plaintext_sha256 character varying(64),
    CONSTRAINT ck_media_files_download_status CHECK (((download_status)::text = ANY ((ARRAY['pending'::character varying, 'downloading'::character varying, 'downloaded'::character varying, 'unavailable'::character varying, 'expired'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_media_files_media_type CHECK (((media_type)::text = ANY ((ARRAY['image'::character varying, 'video'::character varying, 'gif'::character varying, 'audio'::character varying, 'voice_note'::character varying, 'sticker'::character varying, 'document'::character varying, 'unknown'::character varying])::text[])))
);


--
-- Name: media_files_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.media_files_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: media_files_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.media_files_id_seq OWNED BY public.media_files.id;


--
-- Name: message_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.message_segments (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    whatsapp_account_id uuid NOT NULL,
    chat_id bigint NOT NULL,
    chat_jid character varying(128) NOT NULL,
    sequence_number integer NOT NULL,
    drive_file_id character varying(128),
    first_timestamp bigint,
    last_timestamp bigint,
    message_count integer NOT NULL,
    uncompressed_bytes bigint NOT NULL,
    compressed_bytes bigint NOT NULL,
    stored_bytes bigint NOT NULL,
    sha256 character varying(64),
    ciphertext_sha256 character varying(64),
    status character varying(16) NOT NULL,
    format_version integer NOT NULL,
    encrypted boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    uploaded_at timestamp with time zone,
    CONSTRAINT ck_message_segments_status CHECK (((status)::text = ANY ((ARRAY['building'::character varying, 'uploading'::character varying, 'ready'::character varying, 'failed'::character varying])::text[])))
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id bigint NOT NULL,
    chat_id bigint NOT NULL,
    chat_jid character varying(128) NOT NULL,
    whatsapp_message_id character varying(128),
    synthetic_identifier character varying(128),
    sender_jid character varying(128),
    sender_lid character varying(128),
    message_type character varying(32) NOT NULL,
    text text,
    "timestamp" bigint NOT NULL,
    from_me boolean NOT NULL,
    source character varying(20) NOT NULL,
    raw_metadata jsonb,
    raw_proto bytea,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    segment_id uuid,
    segment_index integer,
    storage_status character varying(16) NOT NULL,
    text_truncated boolean NOT NULL,
    CONSTRAINT ck_messages_source CHECK (((source)::text = ANY ((ARRAY['initial_history'::character varying, 'on_demand'::character varying, 'live'::character varying, 'unknown'::character varying])::text[]))),
    CONSTRAINT ck_messages_wamid_not_empty CHECK (((whatsapp_message_id IS NULL) OR (length((whatsapp_message_id)::text) > 0)))
);


--
-- Name: messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.messages_id_seq OWNED BY public.messages.id;


--
-- Name: scanned_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scanned_blobs (
    id uuid NOT NULL,
    whatsapp_account_id uuid NOT NULL,
    sha256 character varying(64) NOT NULL,
    sync_type character varying(32),
    seeds_found bigint NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: storage_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_jobs (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    whatsapp_account_id uuid,
    job_type character varying(24) NOT NULL,
    entity_id character varying(64) NOT NULL,
    status character varying(16) NOT NULL,
    attempts integer NOT NULL,
    next_retry_at timestamp with time zone,
    last_error text,
    payload_bytes bigint NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_storage_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'processing'::character varying, 'complete'::character varying, 'failed'::character varying, 'paused'::character varying])::text[]))),
    CONSTRAINT ck_storage_jobs_type CHECK (((job_type)::text = ANY ((ARRAY['message_segment'::character varying, 'media'::character varying])::text[])))
);


--
-- Name: user_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone,
    ip character varying(64),
    user_agent character varying(255)
);


--
-- Name: user_storage_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_storage_keys (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    encrypted_dek bytea NOT NULL,
    key_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    rotated_at timestamp with time zone
);


--
-- Name: user_whatsapp_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_whatsapp_memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    whatsapp_account_id uuid NOT NULL,
    role character varying(16) DEFAULT 'owner'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_memberships_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'member'::character varying])::text[])))
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email public.citext NOT NULL,
    password_hash text,
    display_name character varying(120),
    avatar_url text,
    auth_provider character varying(16) NOT NULL,
    google_subject character varying(255),
    email_verified boolean NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone,
    CONSTRAINT ck_users_auth_provider CHECK (((auth_provider)::text = ANY ((ARRAY['local'::character varying, 'google'::character varying, 'both'::character varying])::text[]))),
    CONSTRAINT ck_users_tiene_alguna_credencial CHECK (((password_hash IS NOT NULL) OR (google_subject IS NOT NULL)))
);


--
-- Name: whatsapp_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.whatsapp_accounts (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    phone_number character varying(32),
    wa_pn character varying(64),
    wa_lid character varying(64),
    session_status character varying(20) NOT NULL,
    session_storage_key character varying(128) NOT NULL,
    linked_at timestamp with time zone,
    last_connected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_whatsapp_accounts_status CHECK (((session_status)::text = ANY ((ARRAY['never_linked'::character varying, 'linked'::character varying, 'disconnected'::character varying, 'revoked'::character varying, 'error'::character varying])::text[])))
);


--
-- Name: chat_history_state id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_state ALTER COLUMN id SET DEFAULT nextval('public.chat_history_state_id_seq'::regclass);


--
-- Name: chats id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats ALTER COLUMN id SET DEFAULT nextval('public.chats_id_seq'::regclass);


--
-- Name: contacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts ALTER COLUMN id SET DEFAULT nextval('public.contacts_id_seq'::regclass);


--
-- Name: history_requests id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_requests ALTER COLUMN id SET DEFAULT nextval('public.history_requests_id_seq'::regclass);


--
-- Name: media_files id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files ALTER COLUMN id SET DEFAULT nextval('public.media_files_id_seq'::regclass);


--
-- Name: messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages ALTER COLUMN id SET DEFAULT nextval('public.messages_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: app_state app_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_state
    ADD CONSTRAINT app_state_pkey PRIMARY KEY (key);


--
-- Name: chat_history_state chat_history_state_chat_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_state
    ADD CONSTRAINT chat_history_state_chat_id_key UNIQUE (chat_id);


--
-- Name: chat_history_state chat_history_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_state
    ADD CONSTRAINT chat_history_state_pkey PRIMARY KEY (id);


--
-- Name: chats chats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_pkey PRIMARY KEY (id);


--
-- Name: contacts contacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT contacts_pkey PRIMARY KEY (id);


--
-- Name: drive_folders drive_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.drive_folders
    ADD CONSTRAINT drive_folders_pkey PRIMARY KEY (id);


--
-- Name: google_credentials google_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_credentials
    ADD CONSTRAINT google_credentials_pkey PRIMARY KEY (id);


--
-- Name: google_credentials google_credentials_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_credentials
    ADD CONSTRAINT google_credentials_user_id_key UNIQUE (user_id);


--
-- Name: google_drive_storage google_drive_storage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_drive_storage
    ADD CONSTRAINT google_drive_storage_pkey PRIMARY KEY (id);


--
-- Name: google_drive_storage google_drive_storage_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_drive_storage
    ADD CONSTRAINT google_drive_storage_user_id_key UNIQUE (user_id);


--
-- Name: history_requests history_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_requests
    ADD CONSTRAINT history_requests_pkey PRIMARY KEY (id);


--
-- Name: history_seeds history_seeds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_seeds
    ADD CONSTRAINT history_seeds_pkey PRIMARY KEY (id);


--
-- Name: media_files media_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_pkey PRIMARY KEY (id);


--
-- Name: message_segments message_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_segments
    ADD CONSTRAINT message_segments_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: scanned_blobs scanned_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanned_blobs
    ADD CONSTRAINT scanned_blobs_pkey PRIMARY KEY (id);


--
-- Name: storage_jobs storage_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_jobs
    ADD CONSTRAINT storage_jobs_pkey PRIMARY KEY (id);


--
-- Name: chats uq_chats_account_jid; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT uq_chats_account_jid UNIQUE (whatsapp_account_id, jid);


--
-- Name: contacts uq_contacts_account_jid; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT uq_contacts_account_jid UNIQUE (whatsapp_account_id, jid);


--
-- Name: drive_folders uq_drive_folders_user_path; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.drive_folders
    ADD CONSTRAINT uq_drive_folders_user_path UNIQUE (user_id, path);


--
-- Name: history_seeds uq_history_seeds_chat_msg; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_seeds
    ADD CONSTRAINT uq_history_seeds_chat_msg UNIQUE (whatsapp_account_id, chat_id, wa_msg_id);


--
-- Name: media_files uq_media_files_message_type; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT uq_media_files_message_type UNIQUE (message_id, media_type);


--
-- Name: user_whatsapp_memberships uq_memberships_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_whatsapp_memberships
    ADD CONSTRAINT uq_memberships_user UNIQUE (user_id);


--
-- Name: user_whatsapp_memberships uq_memberships_user_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_whatsapp_memberships
    ADD CONSTRAINT uq_memberships_user_account UNIQUE (user_id, whatsapp_account_id);


--
-- Name: message_segments uq_message_segments_chat_seq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_segments
    ADD CONSTRAINT uq_message_segments_chat_seq UNIQUE (chat_id, sequence_number);


--
-- Name: scanned_blobs uq_scanned_blobs_account_sha; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanned_blobs
    ADD CONSTRAINT uq_scanned_blobs_account_sha UNIQUE (whatsapp_account_id, sha256);


--
-- Name: storage_jobs uq_storage_jobs_entity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_jobs
    ADD CONSTRAINT uq_storage_jobs_entity UNIQUE (job_type, entity_id);


--
-- Name: whatsapp_accounts uq_whatsapp_accounts_storage; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whatsapp_accounts
    ADD CONSTRAINT uq_whatsapp_accounts_storage UNIQUE (session_storage_key);


--
-- Name: user_sessions user_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_pkey PRIMARY KEY (id);


--
-- Name: user_sessions user_sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: user_storage_keys user_storage_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_storage_keys
    ADD CONSTRAINT user_storage_keys_pkey PRIMARY KEY (id);


--
-- Name: user_storage_keys user_storage_keys_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_storage_keys
    ADD CONSTRAINT user_storage_keys_user_id_key UNIQUE (user_id);


--
-- Name: user_whatsapp_memberships user_whatsapp_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_whatsapp_memberships
    ADD CONSTRAINT user_whatsapp_memberships_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_google_subject_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_google_subject_key UNIQUE (google_subject);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: whatsapp_accounts whatsapp_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whatsapp_accounts
    ADD CONSTRAINT whatsapp_accounts_pkey PRIMARY KEY (id);


--
-- Name: ix_chat_history_state_jid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_history_state_jid ON public.chat_history_state USING btree (chat_jid);


--
-- Name: ix_chats_last_message_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chats_last_message_timestamp ON public.chats USING btree (last_message_timestamp);


--
-- Name: ix_chats_whatsapp_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chats_whatsapp_account_id ON public.chats USING btree (whatsapp_account_id);


--
-- Name: ix_contacts_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contacts_account ON public.contacts USING btree (whatsapp_account_id);


--
-- Name: ix_contacts_lid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contacts_lid ON public.contacts USING btree (lid);


--
-- Name: ix_contacts_phone_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contacts_phone_number ON public.contacts USING btree (phone_number);


--
-- Name: ix_contacts_push_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_contacts_push_name ON public.contacts USING btree (push_name);


--
-- Name: ix_history_requests_chat_jid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_requests_chat_jid ON public.history_requests USING btree (chat_jid);


--
-- Name: ix_history_requests_protocol_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_requests_protocol_id ON public.history_requests USING btree (protocol_request_id);


--
-- Name: ix_history_requests_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_requests_status ON public.history_requests USING btree (status);


--
-- Name: ix_history_seeds_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_seeds_chat ON public.history_seeds USING btree (chat_id, "timestamp");


--
-- Name: ix_history_seeds_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_seeds_user ON public.history_seeds USING btree (user_id);


--
-- Name: ix_history_state_next_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_state_next_retry ON public.chat_history_state USING btree (next_retry_at);


--
-- Name: ix_history_state_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_history_state_status ON public.chat_history_state USING btree (history_status);


--
-- Name: ix_media_files_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_chat_id ON public.media_files USING btree (chat_id);


--
-- Name: ix_media_files_download_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_download_status ON public.media_files USING btree (download_status);


--
-- Name: ix_media_files_file_sha256; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_media_files_file_sha256 ON public.media_files USING btree (file_sha256);


--
-- Name: ix_memberships_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memberships_account ON public.user_whatsapp_memberships USING btree (whatsapp_account_id);


--
-- Name: ix_message_segments_chat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_message_segments_chat ON public.message_segments USING btree (chat_id, sequence_number);


--
-- Name: ix_message_segments_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_message_segments_status ON public.message_segments USING btree (status);


--
-- Name: ix_message_segments_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_message_segments_user ON public.message_segments USING btree (user_id);


--
-- Name: ix_messages_chat_id_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_messages_chat_id_timestamp ON public.messages USING btree (chat_id, "timestamp");


--
-- Name: ix_messages_chat_jid_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_messages_chat_jid_timestamp ON public.messages USING btree (chat_jid, "timestamp");


--
-- Name: ix_messages_segment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_messages_segment_id ON public.messages USING btree (segment_id);


--
-- Name: ix_messages_sender_jid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_messages_sender_jid ON public.messages USING btree (sender_jid);


--
-- Name: ix_messages_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_messages_source ON public.messages USING btree (source);


--
-- Name: ix_storage_jobs_listos; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_jobs_listos ON public.storage_jobs USING btree (status, next_retry_at);


--
-- Name: ix_storage_jobs_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_storage_jobs_user ON public.storage_jobs USING btree (user_id, status);


--
-- Name: ix_user_sessions_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_expires ON public.user_sessions USING btree (expires_at);


--
-- Name: ix_user_sessions_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_user ON public.user_sessions USING btree (user_id);


--
-- Name: ix_whatsapp_accounts_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_whatsapp_accounts_user ON public.whatsapp_accounts USING btree (user_id);


--
-- Name: uq_messages_chat_wamid; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_messages_chat_wamid ON public.messages USING btree (chat_id, whatsapp_message_id) WHERE (whatsapp_message_id IS NOT NULL);


--
-- Name: chat_history_state chat_history_state_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_history_state
    ADD CONSTRAINT chat_history_state_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;


--
-- Name: chats chats_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chats
    ADD CONSTRAINT chats_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: drive_folders drive_folders_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.drive_folders
    ADD CONSTRAINT drive_folders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: contacts fk_contacts_account; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contacts
    ADD CONSTRAINT fk_contacts_account FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: google_credentials google_credentials_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_credentials
    ADD CONSTRAINT google_credentials_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: google_drive_storage google_drive_storage_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_drive_storage
    ADD CONSTRAINT google_drive_storage_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: history_requests history_requests_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_requests
    ADD CONSTRAINT history_requests_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE SET NULL;


--
-- Name: history_seeds history_seeds_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_seeds
    ADD CONSTRAINT history_seeds_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;


--
-- Name: history_seeds history_seeds_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_seeds
    ADD CONSTRAINT history_seeds_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: history_seeds history_seeds_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.history_seeds
    ADD CONSTRAINT history_seeds_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: media_files media_files_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;


--
-- Name: media_files media_files_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.media_files
    ADD CONSTRAINT media_files_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.messages(id) ON DELETE CASCADE;


--
-- Name: message_segments message_segments_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_segments
    ADD CONSTRAINT message_segments_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;


--
-- Name: message_segments message_segments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_segments
    ADD CONSTRAINT message_segments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: message_segments message_segments_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.message_segments
    ADD CONSTRAINT message_segments_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: messages messages_chat_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.chats(id) ON DELETE CASCADE;


--
-- Name: messages messages_segment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_segment_id_fkey FOREIGN KEY (segment_id) REFERENCES public.message_segments(id) ON DELETE SET NULL;


--
-- Name: scanned_blobs scanned_blobs_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scanned_blobs
    ADD CONSTRAINT scanned_blobs_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: storage_jobs storage_jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_jobs
    ADD CONSTRAINT storage_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: storage_jobs storage_jobs_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_jobs
    ADD CONSTRAINT storage_jobs_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: user_sessions user_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_storage_keys user_storage_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_storage_keys
    ADD CONSTRAINT user_storage_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_whatsapp_memberships user_whatsapp_memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_whatsapp_memberships
    ADD CONSTRAINT user_whatsapp_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_whatsapp_memberships user_whatsapp_memberships_whatsapp_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_whatsapp_memberships
    ADD CONSTRAINT user_whatsapp_memberships_whatsapp_account_id_fkey FOREIGN KEY (whatsapp_account_id) REFERENCES public.whatsapp_accounts(id) ON DELETE CASCADE;


--
-- Name: whatsapp_accounts whatsapp_accounts_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whatsapp_accounts
    ADD CONSTRAINT whatsapp_accounts_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



-- ===========================================================================
-- SEMILLA MINIMA
-- ===========================================================================
--
-- La unica fila obligatoria de toda la base. Sin ella, Alembic cree que la
-- base esta vacia e intentaria aplicar las migraciones sobre un esquema que ya
-- existe, fallando en la primera.
--
-- No hay ninguna otra: ni planes, ni roles, ni catalogos. Los usuarios se dan
-- de alta con Google desde la aplicacion.
INSERT INTO public.alembic_version (version_num)
VALUES ('b2c3d4e5f6a7')
ON CONFLICT DO NOTHING;
