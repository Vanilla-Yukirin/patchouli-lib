import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
export default defineConfig({
	base: "/admin/assets/",
	plugins: [react(), tailwindcss()],
	build: {
		outDir: "../src/patchouli_lib/web_resources",
		assetsDir: "",
		emptyOutDir: true,
	},
	server: { proxy: { "/admin/api": "http://127.0.0.1:8000" } },
});
