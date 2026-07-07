--
-- PostgreSQL database dump
--

\restrict UZPVy7aTDOc1bKIKdmtZdw9ytuxm8zAusyfRgsLcojjY6M57G8AIWJHNVO2rox9

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: allowed_measurements; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.allowed_measurements (
    id text NOT NULL,
    network_id text NOT NULL,
    kind text NOT NULL,
    value text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    added_by_user_id text,
    added_tx text,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT allowed_measurements_kind_check CHECK ((kind = ANY (ARRAY['composeHash'::text, 'appId'::text, 'kmsRoot'::text, 'deviceId'::text])))
);


ALTER TABLE public.allowed_measurements OWNER TO synclave;

--
-- Name: app_audit_events; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.app_audit_events (
    id text NOT NULL,
    org_id text,
    actor_user_id text,
    action text NOT NULL,
    target text,
    metadata jsonb,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    target_id text
);


ALTER TABLE public.app_audit_events OWNER TO synclave;

--
-- Name: apps; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.apps (
    id text NOT NULL,
    org_id text NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'deploying'::text NOT NULL,
    source text NOT NULL,
    ref text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT apps_status_check CHECK ((status = ANY (ARRAY['deploying'::text, 'live'::text, 'failed'::text, 'archived'::text])))
);


ALTER TABLE public.apps OWNER TO synclave;

--
-- Name: billing_accounts; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.billing_accounts (
    org_id text NOT NULL,
    funding_user_id text,
    funding_payment_method text,
    balance_micro bigint DEFAULT 0 NOT NULL,
    metered_through timestamp with time zone,
    autotopup_enabled boolean DEFAULT true NOT NULL,
    autotopup_threshold_micro bigint DEFAULT 5000000 NOT NULL,
    autotopup_amount_micro bigint DEFAULT 20000000 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT billing_accounts_status_check CHECK ((status = ANY (ARRAY['active'::text, 'past_due'::text, 'suspended'::text])))
);


ALTER TABLE public.billing_accounts OWNER TO synclave;

--
-- Name: billing_ledger; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.billing_ledger (
    id text NOT NULL,
    org_id text NOT NULL,
    kind text NOT NULL,
    amount_micro bigint NOT NULL,
    balance_after_micro bigint NOT NULL,
    ref text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT billing_ledger_kind_check CHECK ((kind = ANY (ARRAY['usage_debit'::text, 'topup'::text, 'grant'::text, 'adjustment'::text, 'refund'::text])))
);


ALTER TABLE public.billing_ledger OWNER TO synclave;

--
-- Name: credit_grants; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.credit_grants (
    id text NOT NULL,
    user_id text NOT NULL,
    amount integer NOT NULL,
    source text,
    granted_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.credit_grants OWNER TO synclave;

--
-- Name: cvms; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.cvms (
    id text NOT NULL,
    network_id text NOT NULL,
    name text NOT NULL,
    provider text,
    role text,
    flavor text,
    member_contract_addr text,
    app_id text,
    member_id text,
    measurement text,
    approved_measurement text,
    x_pub_key text,
    wg_pub_key text,
    mesh_ip text,
    status text DEFAULT 'planned'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    registered_at timestamp with time zone,
    last_healthz_at timestamp with time zone,
    last_heartbeat_at timestamp with time zone,
    source text,
    catalog_id text,
    CONSTRAINT cvms_source_check CHECK ((source = ANY (ARRAY['catalog'::text, 'image'::text]))),
    CONSTRAINT cvms_status_check CHECK ((status = ANY (ARRAY['planned'::text, 'allowlisted'::text, 'booting'::text, 'registered'::text, 'live'::text, 'watch'::text, 'down'::text, 'quarantined'::text, 'archived'::text])))
);


ALTER TABLE public.cvms OWNER TO synclave;

--
-- Name: deployments; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.deployments (
    id text NOT NULL,
    org_id text NOT NULL,
    network_id text,
    kind text NOT NULL,
    action text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    phase text,
    target_id text NOT NULL,
    target_name text NOT NULL,
    actor_user_id text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT deployments_action_check CHECK ((action = ANY (ARRAY['deploy'::text, 'redeploy'::text, 'config'::text, 'promote'::text, 'upgrade'::text]))),
    CONSTRAINT deployments_kind_check CHECK ((kind = ANY (ARRAY['app'::text, 'service'::text]))),
    CONSTRAINT deployments_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'building'::text, 'deploying'::text, 'ready'::text, 'error'::text, 'canceled'::text])))
);


ALTER TABLE public.deployments OWNER TO synclave;

--
-- Name: github_identities; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.github_identities (
    id text NOT NULL,
    user_id text NOT NULL,
    github_login text NOT NULL,
    github_id bigint NOT NULL,
    access_token text NOT NULL,
    scope text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.github_identities OWNER TO synclave;

--
-- Name: member_kinds; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.member_kinds (
    network_id text NOT NULL,
    ref text NOT NULL,
    kind text NOT NULL,
    updated_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT member_kinds_kind_check CHECK ((kind = ANY (ARRAY['data'::text, 'agents'::text, 'gateways'::text, 'workers'::text, 'unclassified'::text])))
);


ALTER TABLE public.member_kinds OWNER TO synclave;

--
-- Name: member_names; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.member_names (
    network_id text NOT NULL,
    ref text NOT NULL,
    name text NOT NULL,
    source text NOT NULL,
    preset_name text,
    updated_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    history jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT member_names_name_check CHECK ((btrim(name) <> ''::text)),
    CONSTRAINT member_names_source_check CHECK ((source = ANY (ARRAY['preset'::text, 'user'::text])))
);


ALTER TABLE public.member_names OWNER TO synclave;

--
-- Name: network_signers; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.network_signers (
    id text NOT NULL,
    network_id text NOT NULL,
    user_id text NOT NULL,
    signer_kind text DEFAULT 'privy_wallet'::text NOT NULL,
    signer_address text NOT NULL,
    role text DEFAULT 'trust_signer'::text NOT NULL,
    added_by text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    removed_at timestamp with time zone,
    CONSTRAINT network_signers_role_check CHECK ((role = 'trust_signer'::text)),
    CONSTRAINT network_signers_signer_kind_check CHECK ((signer_kind = 'privy_wallet'::text))
);


ALTER TABLE public.network_signers OWNER TO synclave;

--
-- Name: networks; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.networks (
    id text NOT NULL,
    org_id text NOT NULL,
    name text NOT NULL,
    chain_id integer,
    cluster_diamond_addr text,
    cluster_owner_safe_addr text,
    kms_root_addr text,
    mesh_cidr_ip text,
    mesh_cidr_prefix integer,
    status text DEFAULT 'draft'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    member_count integer DEFAULT 0 NOT NULL,
    last_synced_block bigint,
    CONSTRAINT networks_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'provisioning'::text, 'live'::text, 'degraded'::text, 'archived'::text])))
);


ALTER TABLE public.networks OWNER TO synclave;

--
-- Name: org_memberships; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.org_memberships (
    org_id text NOT NULL,
    user_id text NOT NULL,
    role text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    invited_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    CONSTRAINT org_memberships_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'billing'::text, 'viewer'::text]))),
    CONSTRAINT org_memberships_status_check CHECK ((status = ANY (ARRAY['invited'::text, 'active'::text])))
);


ALTER TABLE public.org_memberships OWNER TO synclave;

--
-- Name: orgs; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.orgs (
    id text NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    kind text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    archived_at timestamp with time zone,
    CONSTRAINT orgs_kind_check CHECK ((kind = ANY (ARRAY['personal'::text, 'team'::text]))),
    CONSTRAINT orgs_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


ALTER TABLE public.orgs OWNER TO synclave;

--
-- Name: privy_identities; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.privy_identities (
    user_id text NOT NULL,
    privy_did text NOT NULL,
    embedded_wallet_addr text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.privy_identities OWNER TO synclave;

--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.schema_migrations (
    id text NOT NULL,
    checksum text,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.schema_migrations OWNER TO synclave;

--
-- Name: users; Type: TABLE; Schema: public; Owner: synclave
--

CREATE TABLE public.users (
    id text NOT NULL,
    email text NOT NULL,
    name text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    credits integer DEFAULT 0 NOT NULL,
    stripe_customer_id text,
    default_team_org_id text,
    CONSTRAINT users_status_check CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text])))
);


ALTER TABLE public.users OWNER TO synclave;

--
-- Data for Name: allowed_measurements; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.allowed_measurements (id, network_id, kind, value, active, added_by_user_id, added_tx, added_at) FROM stdin;
\.


--
-- Data for Name: app_audit_events; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.app_audit_events (id, org_id, actor_user_id, action, target, metadata, ts, target_id) FROM stdin;
aud_yqrejv6v0hf3kjbv	\N	usr_xmxyajaps0dgv5zw	auth.session_created	\N	\N	2026-07-01 23:36:50.238743+00	\N
aud_56f968pzwx1h6kpk	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	org.created	org_jnzw834xgktn32xf	{"name": "a"}	2026-07-01 23:36:53.046049+00	\N
aud_pa0j3qt5e9e43777	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	network.created	net_skgnvr90s6w4qb1e	{"name": "a"}	2026-07-01 23:36:55.444255+00	\N
aud_4qk6t8kfytsxn1wj	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	network.activated	net_skgnvr90s6w4qb1e	\N	2026-07-01 23:36:56.694064+00	\N
aud_eqx96cbcr04fz7t7	\N	usr_xmxyajaps0dgv5zw	github.connected	yolo-maxi	\N	2026-07-01 23:37:01.915184+00	\N
aud_ajtw5203mw5ggfn7	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	app.deployed	waifus	{"ref": "master", "fullName": "yolo-maxi/ai-girlfriend"}	2026-07-01 23:40:19.586253+00	\N
aud_r8r52edbp0yjrvs2	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	app.promoted	waifus	\N	2026-07-01 23:42:50.870679+00	\N
aud_gteffhpdj519scx1	\N	usr_w40ath8jv928vhsz	auth.session_created	\N	\N	2026-07-05 17:12:35.526758+00	\N
aud_9sq4pwtrbkfbxrbx	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	service.deployed	postgres	{"flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost"}	2026-07-06 04:50:44.980879+00	cvm_orch_e42289a09f9dfc7f
aud_yp5v9m8fphrk13gg	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	service.deployed	postgres	{"flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost"}	2026-07-06 04:53:41.383614+00	cvm_orch_60e7e1f39de6a8fd
aud_2jmj9yrkaa7fepam	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	service.deployed	postgres	{"flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost"}	2026-07-06 04:57:42.827587+00	cvm_orch_d44c460662e39c07
aud_a3e2ta6fede4jgq0	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	service.deployed	postgres	{"flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost"}	2026-07-06 06:02:55.346819+00	cvm_orch_d1a69314e3d8d196
aud_nt6340kjtv8hzkxt	org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	service.deployed	postgres	{"flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost"}	2026-07-06 17:07:32.344715+00	cvm_orch_baeff808751d3758
aud_fqavmxy8cz1782tn	org_1b4eae3gn9w372wd	usr_w40ath8jv928vhsz	org.created	org_1b4eae3gn9w372wd	{"name": "Test"}	2026-07-06 19:20:59.321873+00	\N
aud_park8r8b3dhj4n81	org_1b4eae3gn9w372wd	usr_w40ath8jv928vhsz	network.created	net_xyyp3wr99tq7wg0d	{"name": "Default network"}	2026-07-07 00:15:11.421449+00	\N
aud_g15kq52vyqq57jgk	org_1b4eae3gn9w372wd	usr_w40ath8jv928vhsz	network.activated	net_xyyp3wr99tq7wg0d	\N	2026-07-07 00:15:11.421449+00	\N
\.


--
-- Data for Name: apps; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.apps (id, org_id, name, status, source, ref, created_by, created_at, archived_at) FROM stdin;
app_efrskegj08pjn3xa	org_jnzw834xgktn32xf	waifus	live	https://github.com/yolo-maxi/ai-girlfriend.git	master	usr_xmxyajaps0dgv5zw	2026-07-01 23:40:19.586253+00	\N
\.


--
-- Data for Name: billing_accounts; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.billing_accounts (org_id, funding_user_id, funding_payment_method, balance_micro, metered_through, autotopup_enabled, autotopup_threshold_micro, autotopup_amount_micro, status, created_at, updated_at) FROM stdin;
org_1b4eae3gn9w372wd	\N	\N	0	\N	t	5000000	20000000	active	2026-07-06 23:47:04.098267+00	2026-07-06 23:47:04.098267+00
\.


--
-- Data for Name: billing_ledger; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.billing_ledger (id, org_id, kind, amount_micro, balance_after_micro, ref, created_at) FROM stdin;
\.


--
-- Data for Name: credit_grants; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.credit_grants (id, user_id, amount, source, granted_by, created_at) FROM stdin;
crd_n47198ffcsc8h94x	usr_xmxyajaps0dgv5zw	100	lsdan funding (re-grant after DB reset)	\N	2026-07-01 23:38:32.034818+00
\.


--
-- Data for Name: cvms; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.cvms (id, network_id, name, provider, role, flavor, member_contract_addr, app_id, member_id, measurement, approved_measurement, x_pub_key, wg_pub_key, mesh_ip, status, created_by, created_at, registered_at, last_healthz_at, last_heartbeat_at, source, catalog_id) FROM stdin;
\.


--
-- Data for Name: deployments; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.deployments (id, org_id, network_id, kind, action, status, phase, target_id, target_name, actor_user_id, meta, error, created_at, finished_at) FROM stdin;
dep_r2shn1tff2q1dz47	org_jnzw834xgktn32xf	\N	service	deploy	error	failed	cvm_orch_d1a69314e3d8d196	postgres	usr_xmxyajaps0dgv5zw	{"jobId": "d1a69314e3d8d196", "flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost", "catalogName": "Confidential PostgreSQL", "networkName": "Open Webhost"}	Enclave VM disappeared from dstack while waiting for mesh registration.: FATAL: enclave VM disappeared from dstack while waiting for mesh registration (vm=e549551d-3f2c-4e9c-a95d-226961bc7512)	2026-07-06 06:02:55.346819+00	2026-07-06 15:40:32.491331+00
dep_t5g9xmbk394z9jam	org_jnzw834xgktn32xf	\N	service	deploy	error	failed	cvm_orch_d44c460662e39c07	postgres	usr_xmxyajaps0dgv5zw	{"jobId": "d44c460662e39c07", "flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost", "catalogName": "Confidential PostgreSQL", "networkName": "Open Webhost"}	Timed out waiting for the enclave to register as a cluster member. The VM was created and allowlisted, but its sidecar did not join the mesh before the orchestrator timeout.	2026-07-06 04:57:42.827587+00	2026-07-06 15:40:32.495742+00
dep_v05z6vdnawn361ek	org_jnzw834xgktn32xf	\N	service	deploy	error	failed	cvm_orch_60e7e1f39de6a8fd	postgres	usr_xmxyajaps0dgv5zw	{"jobId": "60e7e1f39de6a8fd", "flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost", "catalogName": "Confidential PostgreSQL", "networkName": "Open Webhost"}	Timed out waiting for the enclave to register as a cluster member. The VM was created and allowlisted, but its sidecar did not join the mesh before the orchestrator timeout.	2026-07-06 04:53:41.383614+00	2026-07-06 15:40:32.497875+00
dep_8erfgmvc216wchbq	org_jnzw834xgktn32xf	\N	service	deploy	error	failed	cvm_orch_e42289a09f9dfc7f	postgres	usr_xmxyajaps0dgv5zw	{"jobId": "e42289a09f9dfc7f", "flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost", "catalogName": "Confidential PostgreSQL", "networkName": "Open Webhost"}	Error: HTTP error 403 with body: {"jsonrpc":"2.0","id":0,"error":{"code":-32600,"message":"BASE_MAINNET is not enabled for this app. Visit this page to enable the network: https://dashboard.alchemy.com/apps/l4z5cz88t1dxzkjl/networks"}}	2026-07-06 04:50:44.980879+00	2026-07-06 15:40:32.520858+00
dep_w4gw8jw60k9k3s73	org_jnzw834xgktn32xf	\N	service	deploy	error	failed	cvm_orch_baeff808751d3758	postgres	usr_xmxyajaps0dgv5zw	{"jobId": "baeff808751d3758", "flavor": "small", "source": "catalog", "catalogId": "postgres", "networkId": "net_open_webhost", "catalogName": "Confidential PostgreSQL", "networkName": "Open Webhost"}	[20260706T171025Z] FATAL: enclave VM boot failed before mesh registration: failed to start containers (progress: starting containers, vm=851b7184-51c4-4c93-8ab9-6d802f3e768d)	2026-07-06 17:07:32.344715+00	2026-07-06 17:10:29.091011+00
\.


--
-- Data for Name: github_identities; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.github_identities (id, user_id, github_login, github_id, access_token, scope, created_at, updated_at) FROM stdin;
gh_gmy0ze2eqqqa57m3	usr_xmxyajaps0dgv5zw	yolo-maxi	248328796	v1.rXPV0msxjNo6pTFR.Y-xbPv2SlhM6KTUaFQIrOQ.Bc_M7YJsHWOLXxMVKcWYtCLS-MBPbYBCxIzDb__EAnEu0p8K5NV-0Q	read:user,repo	2026-07-01 23:37:01.915184+00	2026-07-01 23:37:01.915184+00
\.


--
-- Data for Name: member_kinds; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.member_kinds (network_id, ref, kind, updated_by, updated_at) FROM stdin;
net_attestmesh_live	02cafb3c815029a68f8bb34f2a832451c0c1fd84	data	usr_w40ath8jv928vhsz	2026-07-06 02:42:09.653328+00
net_attestmesh_live	d3e18376ec4f7ea9ba75a5018421c9ef01b916a3	gateways	usr_w40ath8jv928vhsz	2026-07-06 02:42:30.727252+00
\.


--
-- Data for Name: member_names; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.member_names (network_id, ref, name, source, preset_name, updated_by, updated_at, history) FROM stdin;
net_open_webhost	f7c613533101782eefdd63b60c7bdb3dd06f8935	runyard	user	\N	box-admin:import:box-console	2026-07-06 06:02:22.404856+00	[{"at": "2026-07-06T06:02:22.414Z", "by": "box-admin:import:box-console", "name": "runyard"}]
net_open_webhost	59fb23d8e9ac29ffaf1ba9ccd1687bc96edd347f	webhost + concierge	user	open-webhost	box-admin:import-test	2026-07-06 06:02:07.97767+00	[{"at": "2026-07-06T06:02:07.987Z", "by": "box-admin:import-test", "name": "webhost + concierge"}]
net_open_webhost	f7c6135331ae3bd711cbd5a258d25d4c66e35be8	runyard	user	open-webhost-runyard	box-admin:box-admin	2026-07-06 06:09:24.516752+00	[{"at": "2026-07-06T06:03:05.232Z", "by": "box-admin:import:box-console", "name": "runyard"}, {"at": "2026-07-06T06:09:24.358Z", "by": "box-admin:box-admin", "name": "runyard (prod-smoke)"}, {"at": "2026-07-06T06:09:24.520Z", "by": "box-admin:box-admin", "name": "runyard"}]
\.


--
-- Data for Name: network_signers; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.network_signers (id, network_id, user_id, signer_kind, signer_address, role, added_by, added_at, removed_at) FROM stdin;
\.


--
-- Data for Name: networks; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.networks (id, org_id, name, chain_id, cluster_diamond_addr, cluster_owner_safe_addr, kms_root_addr, mesh_cidr_ip, mesh_cidr_prefix, status, created_by, created_at, archived_at, member_count, last_synced_block) FROM stdin;
net_skgnvr90s6w4qb1e	org_jnzw834xgktn32xf	a	\N	\N	\N	\N	\N	\N	live	usr_xmxyajaps0dgv5zw	2026-07-01 23:36:55.444255+00	\N	0	\N
net_xyyp3wr99tq7wg0d	org_1b4eae3gn9w372wd	Default network	\N	\N	\N	\N	\N	\N	live	usr_w40ath8jv928vhsz	2026-07-07 00:15:11.421449+00	\N	0	\N
\.


--
-- Data for Name: org_memberships; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.org_memberships (org_id, user_id, role, status, invited_by, created_at, activated_at) FROM stdin;
org_88rybmvaz3tv90y9	usr_xmxyajaps0dgv5zw	owner	active	\N	2026-07-01 23:36:50.238743+00	2026-07-01 23:36:50.238743+00
org_jnzw834xgktn32xf	usr_xmxyajaps0dgv5zw	owner	active	\N	2026-07-01 23:36:53.046049+00	2026-07-01 23:36:53.046049+00
org_re6pwxz6ztew7pvn	usr_w40ath8jv928vhsz	owner	active	\N	2026-07-05 17:12:35.526758+00	2026-07-05 17:12:35.526758+00
org_1b4eae3gn9w372wd	usr_w40ath8jv928vhsz	owner	active	\N	2026-07-06 19:20:59.321873+00	2026-07-06 19:20:59.321873+00
\.


--
-- Data for Name: orgs; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.orgs (id, name, slug, kind, status, created_by, created_at, archived_at) FROM stdin;
org_88rybmvaz3tv90y9	Personal	oceanvael-v5zw	personal	active	usr_xmxyajaps0dgv5zw	2026-07-01 23:36:50.238743+00	\N
org_jnzw834xgktn32xf	a	a-1tyr	team	active	usr_xmxyajaps0dgv5zw	2026-07-01 23:36:53.046049+00	\N
org_re6pwxz6ztew7pvn	Personal	kristel-alliksaar-vhsz	personal	active	usr_w40ath8jv928vhsz	2026-07-05 17:12:35.526758+00	\N
org_1b4eae3gn9w372wd	Test	test-6anm	team	active	usr_w40ath8jv928vhsz	2026-07-06 19:20:59.321873+00	\N
\.


--
-- Data for Name: privy_identities; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.privy_identities (user_id, privy_did, embedded_wallet_addr, created_at, updated_at) FROM stdin;
usr_xmxyajaps0dgv5zw	did:privy:cmr05n1pc00hi0clalg8oc3ki	0x2688A6C4d33Dd96b9ed9930832c2F7fB42D75e8f	2026-07-01 23:36:50.238743+00	2026-07-01 23:36:50.238743+00
usr_w40ath8jv928vhsz	did:privy:cmqr1hwl9000p0die3zgng7kl	0x95Fa739D53bF041D6d8e06e2EE1E70F580Ac4372	2026-07-05 17:12:35.526758+00	2026-07-05 17:12:35.526758+00
\.


--
-- Data for Name: schema_migrations; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.schema_migrations (id, checksum, applied_at) FROM stdin;
0001_identity.sql	e69f0e2b9f264f28a3a3e412c46e548cd6926fcb210973797731d5965bd8a3e1	2026-07-01 20:59:32.039428+00
0002_orgs.sql	b5be28e71f4636d443fc2b262a4455fcf93de579b71debc94ee1f905e0c8b585	2026-07-01 20:59:32.063085+00
0003_networks.sql	33e9f7f6fd9b8c3af7f29f07518094684ecae08a0f47b3c6bd90e59321cb4180	2026-07-01 20:59:32.110436+00
0004_privy_identity.sql	ef4a217374a61c2bb9e579caf3d98c5d0e82728f402ac0e8a981278eb4d3e5af	2026-07-01 20:59:32.129573+00
0005_records.sql	074746b35f1bc9586a9fada4012acd65394c0a6c8159ef76192009f1672138b8	2026-07-01 20:59:32.150167+00
0006_credits.sql	3c43ba532292fc828422a32a5bc54d72b5af46e3519f286daa35ee3777a645ec	2026-07-01 20:59:32.219003+00
0007_github_identities.sql	4f0b47eb477c2ffcc210df8d485a1dff5afafe8433dcbbdb2aedb818303e559e	2026-07-01 20:59:32.243027+00
0008_apps.sql	7cee78e1a03d58c27ff73deceb1c2259d43f853a4fad5a004de6134be3e622fe	2026-07-01 20:59:32.275841+00
0009_usage_billing.sql	38287080759a4a90f36ee2ab6c42d91a8ba1b6f1655e7dbd730da16b55ddb532	2026-07-01 23:15:34.706447+00
0010_apps_name_active_unique.sql	66c77be63bcef294368055ff84c7193e8bef61ff9777e9924170d551d92efafb	2026-07-03 06:20:41.491604+00
0011_audit_target_id.sql	1a21be9feb851f37f7e86b0e5115e4abeb67d32da85b0fdccc35c1448c2ce3d4	2026-07-03 06:20:41.548459+00
0012_deployments.sql	af6f8ba47045efde7832b7e2d0a24b3d64961653607c4906c998c1561271e9a1	2026-07-03 06:20:41.565424+00
0014_deployment_mode_backfill.sql	9d2caf5ca3f8484209e6e489181280cb4bd01b9678a93d02e2000f0a6cfe1d7e	2026-07-05 17:05:07.840376+00
0015_member_kinds.sql	17d52bf31135fb613f29fc772cc2a62b1afcfe53d33cc17aa6beef8ae2ce51a4	2026-07-05 20:32:22.259568+00
0016_member_names.sql	23809652ec2e0d1df52441498cd0ae81507cec5ed4c4352d9c53f5d023f04547	2026-07-06 06:01:07.920481+00
0017_default_team.sql	7195227676a63b39bb071b5442eba928ebb039516cb92176bc71e3896b105e29	2026-07-06 18:41:05.176685+00
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: synclave
--

COPY public.users (id, email, name, status, created_at, credits, stripe_customer_id, default_team_org_id) FROM stdin;
usr_system	system@fleet.local	system	active	2026-07-01 20:59:32.150167+00	0	\N	\N
usr_xmxyajaps0dgv5zw	oceanvael@gmail.com	oceanvael	active	2026-07-01 23:36:50.238743+00	100	\N	\N
usr_w40ath8jv928vhsz	kristel.alliksaar@gmail.com	kristel.alliksaar	active	2026-07-05 17:12:35.526758+00	0	\N	org_1b4eae3gn9w372wd
\.


--
-- Name: allowed_measurements allowed_measurements_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.allowed_measurements
    ADD CONSTRAINT allowed_measurements_pkey PRIMARY KEY (id);


--
-- Name: app_audit_events app_audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.app_audit_events
    ADD CONSTRAINT app_audit_events_pkey PRIMARY KEY (id);


--
-- Name: apps apps_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.apps
    ADD CONSTRAINT apps_pkey PRIMARY KEY (id);


--
-- Name: billing_accounts billing_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.billing_accounts
    ADD CONSTRAINT billing_accounts_pkey PRIMARY KEY (org_id);


--
-- Name: billing_ledger billing_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.billing_ledger
    ADD CONSTRAINT billing_ledger_pkey PRIMARY KEY (id);


--
-- Name: credit_grants credit_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.credit_grants
    ADD CONSTRAINT credit_grants_pkey PRIMARY KEY (id);


--
-- Name: cvms cvms_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.cvms
    ADD CONSTRAINT cvms_pkey PRIMARY KEY (id);


--
-- Name: deployments deployments_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_pkey PRIMARY KEY (id);


--
-- Name: github_identities github_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.github_identities
    ADD CONSTRAINT github_identities_pkey PRIMARY KEY (id);


--
-- Name: github_identities github_identities_user_id_key; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.github_identities
    ADD CONSTRAINT github_identities_user_id_key UNIQUE (user_id);


--
-- Name: member_kinds member_kinds_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.member_kinds
    ADD CONSTRAINT member_kinds_pkey PRIMARY KEY (network_id, ref);


--
-- Name: member_names member_names_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.member_names
    ADD CONSTRAINT member_names_pkey PRIMARY KEY (network_id, ref);


--
-- Name: network_signers network_signers_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.network_signers
    ADD CONSTRAINT network_signers_pkey PRIMARY KEY (id);


--
-- Name: networks networks_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.networks
    ADD CONSTRAINT networks_pkey PRIMARY KEY (id);


--
-- Name: org_memberships org_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_pkey PRIMARY KEY (org_id, user_id);


--
-- Name: orgs orgs_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.orgs
    ADD CONSTRAINT orgs_pkey PRIMARY KEY (id);


--
-- Name: privy_identities privy_identities_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.privy_identities
    ADD CONSTRAINT privy_identities_pkey PRIMARY KEY (user_id);


--
-- Name: privy_identities privy_identities_privy_did_key; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.privy_identities
    ADD CONSTRAINT privy_identities_privy_did_key UNIQUE (privy_did);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_stripe_customer_id_key; Type: CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_stripe_customer_id_key UNIQUE (stripe_customer_id);


--
-- Name: allowed_measurements_network_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX allowed_measurements_network_idx ON public.allowed_measurements USING btree (network_id);


--
-- Name: app_audit_events_org_ts_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX app_audit_events_org_ts_idx ON public.app_audit_events USING btree (org_id, ts DESC);


--
-- Name: app_audit_events_target_id_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX app_audit_events_target_id_idx ON public.app_audit_events USING btree (target_id, ts DESC) WHERE (target_id IS NOT NULL);


--
-- Name: apps_name_active_key; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX apps_name_active_key ON public.apps USING btree (name) WHERE (status <> 'archived'::text);


--
-- Name: apps_org_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX apps_org_idx ON public.apps USING btree (org_id) WHERE (status <> 'archived'::text);


--
-- Name: billing_ledger_org_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX billing_ledger_org_idx ON public.billing_ledger USING btree (org_id, created_at DESC);


--
-- Name: billing_ledger_topup_uq; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX billing_ledger_topup_uq ON public.billing_ledger USING btree (ref) WHERE (kind = 'topup'::text);


--
-- Name: credit_grants_user_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX credit_grants_user_idx ON public.credit_grants USING btree (user_id);


--
-- Name: cvms_network_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX cvms_network_idx ON public.cvms USING btree (network_id) WHERE (status <> 'archived'::text);


--
-- Name: deployments_active_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX deployments_active_idx ON public.deployments USING btree (org_id) WHERE (status = ANY (ARRAY['queued'::text, 'building'::text, 'deploying'::text]));


--
-- Name: deployments_org_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX deployments_org_idx ON public.deployments USING btree (org_id, created_at DESC);


--
-- Name: deployments_target_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX deployments_target_idx ON public.deployments USING btree (target_id, created_at DESC);


--
-- Name: github_identities_github_id_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX github_identities_github_id_idx ON public.github_identities USING btree (github_id);


--
-- Name: network_signers_active_uq; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX network_signers_active_uq ON public.network_signers USING btree (network_id, signer_address) WHERE (removed_at IS NULL);


--
-- Name: network_signers_network_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX network_signers_network_idx ON public.network_signers USING btree (network_id) WHERE (removed_at IS NULL);


--
-- Name: networks_org_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX networks_org_idx ON public.networks USING btree (org_id) WHERE (status <> 'archived'::text);


--
-- Name: org_memberships_user_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX org_memberships_user_idx ON public.org_memberships USING btree (user_id);


--
-- Name: orgs_one_personal_per_user_uq; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX orgs_one_personal_per_user_uq ON public.orgs USING btree (created_by) WHERE (kind = 'personal'::text);


--
-- Name: orgs_slug_live_uq; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX orgs_slug_live_uq ON public.orgs USING btree (slug) WHERE (status = 'active'::text);


--
-- Name: users_default_team_org_idx; Type: INDEX; Schema: public; Owner: synclave
--

CREATE INDEX users_default_team_org_idx ON public.users USING btree (default_team_org_id) WHERE (default_team_org_id IS NOT NULL);


--
-- Name: users_email_uq; Type: INDEX; Schema: public; Owner: synclave
--

CREATE UNIQUE INDEX users_email_uq ON public.users USING btree (lower(email));


--
-- Name: allowed_measurements allowed_measurements_added_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.allowed_measurements
    ADD CONSTRAINT allowed_measurements_added_by_user_id_fkey FOREIGN KEY (added_by_user_id) REFERENCES public.users(id);


--
-- Name: allowed_measurements allowed_measurements_network_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.allowed_measurements
    ADD CONSTRAINT allowed_measurements_network_id_fkey FOREIGN KEY (network_id) REFERENCES public.networks(id);


--
-- Name: app_audit_events app_audit_events_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.app_audit_events
    ADD CONSTRAINT app_audit_events_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id);


--
-- Name: app_audit_events app_audit_events_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.app_audit_events
    ADD CONSTRAINT app_audit_events_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: apps apps_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.apps
    ADD CONSTRAINT apps_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: apps apps_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.apps
    ADD CONSTRAINT apps_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: billing_accounts billing_accounts_funding_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.billing_accounts
    ADD CONSTRAINT billing_accounts_funding_user_id_fkey FOREIGN KEY (funding_user_id) REFERENCES public.users(id);


--
-- Name: billing_accounts billing_accounts_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.billing_accounts
    ADD CONSTRAINT billing_accounts_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: billing_ledger billing_ledger_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.billing_ledger
    ADD CONSTRAINT billing_ledger_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: credit_grants credit_grants_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.credit_grants
    ADD CONSTRAINT credit_grants_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.users(id);


--
-- Name: credit_grants credit_grants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.credit_grants
    ADD CONSTRAINT credit_grants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: cvms cvms_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.cvms
    ADD CONSTRAINT cvms_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: cvms cvms_network_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.cvms
    ADD CONSTRAINT cvms_network_id_fkey FOREIGN KEY (network_id) REFERENCES public.networks(id);


--
-- Name: deployments deployments_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users(id);


--
-- Name: deployments deployments_network_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_network_id_fkey FOREIGN KEY (network_id) REFERENCES public.networks(id);


--
-- Name: deployments deployments_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.deployments
    ADD CONSTRAINT deployments_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: github_identities github_identities_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.github_identities
    ADD CONSTRAINT github_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: network_signers network_signers_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.network_signers
    ADD CONSTRAINT network_signers_added_by_fkey FOREIGN KEY (added_by) REFERENCES public.users(id);


--
-- Name: network_signers network_signers_network_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.network_signers
    ADD CONSTRAINT network_signers_network_id_fkey FOREIGN KEY (network_id) REFERENCES public.networks(id);


--
-- Name: network_signers network_signers_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.network_signers
    ADD CONSTRAINT network_signers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: networks networks_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.networks
    ADD CONSTRAINT networks_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: networks networks_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.networks
    ADD CONSTRAINT networks_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: org_memberships org_memberships_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.users(id);


--
-- Name: org_memberships org_memberships_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.orgs(id);


--
-- Name: org_memberships org_memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT org_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: orgs orgs_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.orgs
    ADD CONSTRAINT orgs_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: privy_identities privy_identities_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.privy_identities
    ADD CONSTRAINT privy_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: users users_default_team_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: synclave
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_default_team_org_id_fkey FOREIGN KEY (default_team_org_id) REFERENCES public.orgs(id);


--
-- PostgreSQL database dump complete
--

\unrestrict UZPVy7aTDOc1bKIKdmtZdw9ytuxm8zAusyfRgsLcojjY6M57G8AIWJHNVO2rox9

