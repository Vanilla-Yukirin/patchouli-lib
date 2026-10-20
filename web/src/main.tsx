import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
	createRootRoute,
	createRoute,
	createRouter,
	Outlet,
	RouterProvider,
} from "@tanstack/react-router";
import { setNonce } from "get-nonce";
import React from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "./api";
import { App } from "./app";
import { PreferencesProvider } from "./preferences";
import "./styles.css";

setNonce(
	document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content ||
		"",
);

const queryClient = new QueryClient({
	defaultOptions: {
		queries: {
			retry: (failureCount, error) =>
				failureCount < 1 &&
				!(error instanceof ApiError && error.status === 401),
			staleTime: 15_000,
			refetchOnWindowFocus: false,
		},
		mutations: { retry: false, gcTime: 0 },
	},
});
const rootRoute = createRootRoute({ component: () => <Outlet /> });
const page = createRoute({
	getParentRoute: () => rootRoute,
	path: "$",
	component: App,
});
const index = createRoute({
	getParentRoute: () => rootRoute,
	path: "/",
	component: App,
});
export const adminBase = location.pathname.startsWith("/admin/next")
	? "/admin/next"
	: "/admin";
const router = createRouter({
	routeTree: rootRoute.addChildren([index, page]),
	basepath: adminBase,
});
const container = document.getElementById("root");
if (!container) throw new Error("Application root missing");
createRoot(container).render(
	<React.StrictMode>
		<QueryClientProvider client={queryClient}>
			<PreferencesProvider>
				<RouterProvider router={router} />
			</PreferencesProvider>
		</QueryClientProvider>
	</React.StrictMode>,
);
