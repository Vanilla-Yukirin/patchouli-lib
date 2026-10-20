import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import {
	BookOpen,
	Command,
	HelpCircle,
	KeyRound,
	LogOut,
	Menu,
	Moon,
	Search,
	Settings,
	Sun,
	Tags,
	Trash2,
} from "lucide-react";
import { type FormEvent, Fragment, useEffect, useState } from "react";
import {
	ApiError,
	api,
	request,
	SESSION_EXPIRED_EVENT,
	type Session,
	setSessionCsrf,
} from "./api";
import { CredentialManager } from "./credentials";
import {
	Dashboard,
	HelpPage,
	Libraries,
	LibraryOverview,
	RequestsPage,
	SectionWorkspace,
	SettingsPage,
} from "./pages";
import { usePreferences } from "./preferences";
import { SearchWorkspace } from "./search";
import { TagDetail } from "./tag-detail";
import { TagsManager } from "./tags";
import { TrashManager } from "./trash";
import { ErrorNotice, Loading, Modal, Panel } from "./ui";
import { DocumentWorkspace, SkillWorkspace } from "./workspace";
export const nonce =
	document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content ||
	"";
export function App() {
	const queryClient = useQueryClient();
	const navigate = useNavigate();
	const pathname = useRouterState({ select: (s) => s.location.pathname });
	// TanStack exposes the application-relative pathname after its basepath rewrite.
	const path = pathname || "/";
	const { t, locale, setLocale, dark, setDark } = usePreferences();
	const [menu, setMenu] = useState(false);
	const [command, setCommand] = useState(false);
	const [notice, setNotice] = useState("");
	const [sessionExpired, setSessionExpired] = useState(false);
	const session = useQuery({
		queryKey: ["session"],
		queryFn: api.session,
		retry: false,
	});
	const go = (destination: string) => {
		navigate({ to: destination });
		setMenu(false);
		setCommand(false);
	};
	useEffect(() => {
		if (session.data) setSessionCsrf(session.data.csrf_token || "");
	}, [session.data]);
	useEffect(() => {
		const expire = () => {
			if (!queryClient.getQueryData<Session>(["session"])?.authenticated)
				return;
			setSessionCsrf("");
			setSessionExpired(true);
			setNotice("");
			setMenu(false);
			setCommand(false);
			queryClient.clear();
			queryClient.setQueryData(["session"], { authenticated: false });
		};
		window.addEventListener(SESSION_EXPIRED_EVENT, expire);
		return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expire);
	}, [queryClient]);
	useEffect(() => {
		const handler = (event: KeyboardEvent) => {
			if ((event.ctrlKey || event.metaKey) && event.key === "k") {
				event.preventDefault();
				setCommand((value) => !value);
			}
		};
		window.addEventListener("keydown", handler);
		return () => window.removeEventListener("keydown", handler);
	}, []);
	const done = (message: string) => {
		setNotice(message);
		queryClient.invalidateQueries({
			predicate: (q) => q.queryKey[0] !== "session",
		});
	};
	if (session.isPending) return <Loading />;
	if (!session.data?.authenticated)
		return (
			<Auth
				session={session.data}
				expired={sessionExpired}
				onSuccess={(value) => {
					setSessionExpired(false);
					setSessionCsrf(value.csrf_token || "", true);
					queryClient.setQueryData(["session"], value);
				}}
			/>
		);
	const sections = [
		{
			label: t("app.content"),
			items: [
				["/", t("app.home"), BookOpen],
				["/libraries", t("app.libraries"), BookOpen],
				["/search", t("app.search"), Search],
				["/tags", t("app.tags"), Tags],
				["/trash", t("app.trash"), Trash2],
			],
		},
		{
			label: t("app.manage"),
			items: [
				["/credentials", t("app.credentials"), KeyRound],
				["/requests", t("app.requests"), BookOpen],
				["/settings", t("app.settings"), Settings],
			],
		},
		{
			label: t("app.connect"),
			items: [
				["/help", t("app.guide"), HelpCircle],
				["/help/agent", "Agent", HelpCircle],
				["/help/mcp", "MCP", HelpCircle],
			],
		},
	];
	const navigation = (
		<>
			{sections.map((group) => (
				<div className="nav-group" key={group.label}>
					<span>{group.label}</span>
					{group.items.map(([href, label, Icon]) => (
						<button
							type="button"
							key={String(href)}
							className={path === href ? "nav-item active" : "nav-item"}
							onClick={() => go(String(href))}
						>
							{typeof Icon !== "string" && <Icon size={17} />}
							<span>{String(label)}</span>
						</button>
					))}
				</div>
			))}
		</>
	);
	const props = { go, done, nonce };
	let content: React.ReactNode;
	const parts = path.split("/").filter(Boolean);
	if (path === "/") content = <Dashboard {...props} />;
	else if (path === "/libraries") content = <Libraries {...props} />;
	else if (parts[0] === "libraries" && parts[1]) {
		const libraryId = parts[1];
		if (parts[2] === "pages")
			content = (
				<DocumentWorkspace
					{...props}
					libraryId={libraryId}
					pageId={parts[3]}
					revision={parts[4] === "revisions" ? parts[5] : undefined}
					editing={parts[4] === "edit"}
				/>
			);
		else if (parts[2] === "skills")
			content = (
				<SkillWorkspace
					{...props}
					libraryId={libraryId}
					skillId={parts[3]}
					revision={parts[4] === "revisions" ? parts[5] : undefined}
					editing={parts[3] === "new" || parts[4] === "edit"}
				/>
			);
		else if (parts[2] === "search")
			content = <SearchWorkspace {...props} libraryId={libraryId} />;
		else if (parts[2] === "tags" && parts[3])
			content = <TagDetail {...props} libraryId={libraryId} tagId={parts[3]} />;
		else if (parts[2] === "sections")
			content = (
				<SectionWorkspace
					{...props}
					libraryId={libraryId}
					sectionId={parts[3]}
					bookId={parts[4] === "books" ? parts[5] : undefined}
				/>
			);
		else content = <LibraryOverview {...props} libraryId={libraryId} />;
	} else if (parts[0] === "search") content = <SearchWorkspace {...props} />;
	else if (parts[0] === "tags") content = <TagsManager {...props} />;
	else if (parts[0] === "trash") content = <TrashManager {...props} />;
	else if (parts[0] === "credentials")
		content = <CredentialManager {...props} credentialId={parts[1]} />;
	else if (parts[0] === "requests") content = <RequestsPage {...props} />;
	else if (parts[0] === "settings") content = <SettingsPage {...props} />;
	else if (parts[0] === "help")
		content = <HelpPage {...props} page={parts[1]} />;
	else
		content = (
			<Panel title="404">
				<p>{t("app.pageNotFound")}</p>
			</Panel>
		);
	return (
		<>
			<header className="topbar">
				<button type="button" className="brand" onClick={() => go("/")}>
					<BookOpen />
					Patchouli<span>Lib</span>
				</button>
				<nav className="desktop-nav">
					{sections.map((group) => (
						<DropdownMenu.Root key={group.label}>
							<DropdownMenu.Trigger className="nav-item">
								{group.label}
							</DropdownMenu.Trigger>
							<DropdownMenu.Portal>
								<DropdownMenu.Content
									className="navigation-popover"
									sideOffset={10}
									align="start"
								>
									{group.items.map(([href, label, Icon]) => (
										<DropdownMenu.Item
											key={String(href)}
											className={path === href ? "nav-item active" : "nav-item"}
											onSelect={() => go(String(href))}
										>
											{typeof Icon !== "string" && <Icon size={17} />}
											<span>{String(label)}</span>
										</DropdownMenu.Item>
									))}
								</DropdownMenu.Content>
							</DropdownMenu.Portal>
						</DropdownMenu.Root>
					))}
				</nav>
				<div className="top-actions">
					<button
						type="button"
						className="icon-button"
						onClick={() => setCommand(true)}
						aria-label={t("app.searchAndCommands")}
					>
						<Command size={18} />
					</button>
					<button
						type="button"
						className="icon-button"
						onClick={() => setLocale(locale === "en" ? "zh-CN" : "en")}
					>
						{locale === "en" ? "中" : "EN"}
					</button>
					<button
						type="button"
						className="icon-button"
						onClick={() => setDark(!dark)}
						aria-label={t("app.switchTheme")}
					>
						{dark ? <Sun size={18} /> : <Moon size={18} />}
					</button>
					<button
						type="button"
						className="icon-button"
						aria-label={t("app.signOut")}
						onClick={async () => {
							try {
								await api.logout();
								setSessionCsrf("");
								queryClient.clear();
								session.refetch();
							} catch (error) {
								if (
									!(
										error instanceof ApiError &&
										error.status === 401 &&
										error.code === "sign_in_required"
									)
								)
									setNotice(error instanceof Error ? error.message : "Error");
							}
						}}
					>
						<LogOut size={18} />
					</button>
					<button
						type="button"
						className="icon-button mobile-menu"
						onClick={() => setMenu(true)}
						aria-label={t("app.navigation")}
					>
						<Menu />
					</button>
				</div>
			</header>
			<main className="application">
				<Fragment key={pathname}>{content}</Fragment>
			</main>
			{notice && (
				<div className="toast" role="status">
					<span>{notice}</span>
					<button type="button" onClick={() => setNotice("")}>
						×
					</button>
				</div>
			)}
			<Modal
				open={menu}
				onOpenChange={setMenu}
				title={t("app.navigation")}
				drawer
			>
				{navigation}
			</Modal>
			<Modal
				open={command}
				onOpenChange={setCommand}
				title={t("app.searchAndCommands2")}
			>
				<form
					onSubmit={(event) => {
						event.preventDefault();
						const value = new FormData(event.currentTarget).get("q");
						go(
							(parts[0] === "libraries" && parts[1]
								? `/libraries/${parts[1]}/search`
								: "/search") +
								"?q=" +
								encodeURIComponent(String(value)),
						);
					}}
				>
					<input name="q" autoFocus placeholder={t("app.searchDocuments")} />
					<button type="submit">{t("app.search")}</button>
				</form>
				<div className="command-links">
					<button type="button" onClick={() => go("/libraries")}>
						{t("app.openLibraries")}
					</button>
					<button type="button" onClick={() => go("/credentials")}>
						{t("app.manageCredentials")}
					</button>
				</div>
			</Modal>
		</>
	);
}
function Auth({
	session,
	expired = false,
	onSuccess,
}: {
	session?: Session;
	expired?: boolean;
	onSuccess: (session: Session) => void;
}) {
	const { t, locale, setLocale } = usePreferences();
	const [error, setError] = useState<unknown>();
	const [pending, setPending] = useState(false);
	const setup = Boolean(session?.setup_required);
	const submit = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		const form = event.currentTarget;
		const data = new FormData(form);
		const token = String(data.get("token"));
		if (setup && token !== data.get("confirmation")) {
			setError(new Error(t("app.tokensDoNotMatch")));
			return;
		}
		setPending(true);
		setError(undefined);
		try {
			if (setup) setSessionCsrf(session?.setup_csrf_token || "");
			const value = setup
				? await request<Session>("setup", {
						method: "POST",
						body: JSON.stringify({
							master_token: token,
							confirmation: data.get("confirmation"),
							setup_token: data.get("setup_proof"),
						}),
					})
				: await api.login(token);
			form.reset();
			onSuccess(value);
		} catch (error) {
			setError(error);
		} finally {
			setPending(false);
		}
	};
	return (
		<div className="auth-screen">
			<form className="auth-card" onSubmit={submit}>
				<div className="panel-heading">
					<BookOpen size={30} />
					<button
						type="button"
						className="secondary"
						onClick={() => setLocale(locale === "en" ? "zh-CN" : "en")}
					>
						中文 / English
					</button>
				</div>
				<h1>{setup ? t("app.setUpMasterToken") : t("app.welcomeBack")}</h1>
				<p className="muted">{t("app.aQuietPlaceForYourKnowledge")}</p>
				{expired && (
					<p className="alert" role="alert">
						{locale === "zh-CN"
							? "会话已失效，未保存的输入已清除。请重新登录，并核对尚未确认的操作。"
							: "Your session has ended. Unsaved input has been cleared. Sign in again and check any operation whose outcome was not confirmed."}
					</p>
				)}
				<label>
					{t("app.masterToken")}
					<input
						name="token"
						type="password"
						autoComplete={setup ? "new-password" : "current-password"}
						required
					/>
				</label>
				{setup && (
					<>
						<label>
							{t("app.confirmToken")}
							<input
								name="confirmation"
								type="password"
								autoComplete="new-password"
								required
							/>
						</label>
						<label>
							{t("app.setupProofFromYourDeployer")}
							<input
								name="setup_proof"
								type="password"
								autoComplete="off"
								required
							/>
						</label>
					</>
				)}
				{Boolean(error) && <ErrorNotice error={error} />}
				<button type="submit" className="primary full" disabled={pending}>
					{pending
						? t("app.pleaseWait")
						: setup
							? t("app.finishSetup")
							: t("app.signIn")}
				</button>
			</form>
		</div>
	);
}
export type PageProps = {
	go: (path: string) => void;
	done: (message: string) => void;
	nonce: string;
};
