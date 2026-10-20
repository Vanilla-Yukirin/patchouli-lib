import i18n from "i18next";
import {
	createContext,
	type ReactNode,
	useContext,
	useEffect,
	useState,
} from "react";
import { initReactI18next, useTranslation } from "react-i18next";
import { translations } from "./translations";

void i18n.use(initReactI18next).init({
	resources: {
		en: { translation: translations.en },
		"zh-CN": { translation: translations["zh-CN"] },
	},
	lng: "en",
	fallbackLng: "en",
	keySeparator: false,
	interpolation: { escapeValue: false },
});
export type Locale = "en" | "zh-CN";
type Preferences = {
	locale: Locale;
	setLocale: (value: Locale) => void;
	dark: boolean;
	setDark: (value: boolean) => void;
	t: (key: string, values?: Record<string, unknown>) => string;
};
const Context = createContext<Preferences | null>(null);
function read(key: string) {
	try {
		return localStorage.getItem(key);
	} catch {
		return null;
	}
}
export function PreferencesProvider({ children }: { children: ReactNode }) {
	const { t } = useTranslation();
	const [locale, updateLocale] = useState<Locale>(() =>
		read("patchouli.locale") === "en"
			? "en"
			: read("patchouli.locale") === "zh-CN" ||
					navigator.language.startsWith("zh")
				? "zh-CN"
				: "en",
	);
	const [dark, updateDark] = useState(() => read("patchouli.theme") === "dark");
	useEffect(() => {
		document.documentElement.lang = locale;
		void i18n.changeLanguage(locale);
		document.documentElement.classList.toggle("dark", dark);
	}, [locale, dark]);
	const setLocale = (value: Locale) => {
		updateLocale(value);
		try {
			localStorage.setItem("patchouli.locale", value);
		} catch {}
	};
	const setDark = (value: boolean) => {
		updateDark(value);
		try {
			localStorage.setItem("patchouli.theme", value ? "dark" : "light");
		} catch {}
	};
	return (
		<Context.Provider
			value={{
				locale,
				setLocale,
				dark,
				setDark,
				t: (key, values) => String(t(key, values)),
			}}
		>
			{children}
		</Context.Provider>
	);
}
export function usePreferences() {
	const value = useContext(Context);
	if (!value) throw new Error("Preferences provider missing");
	return value;
}
export function LocalTime({ value }: { value: number | string }) {
	const { locale } = usePreferences();
	const date = new Date(typeof value === "number" ? value / 1000 : value);
	if (Number.isNaN(date.getTime())) return <span>—</span>;
	return (
		<time dateTime={date.toISOString()} title={date.toISOString()}>
			{new Intl.DateTimeFormat(locale, {
				dateStyle: "medium",
				timeStyle: "short",
			}).format(date)}
		</time>
	);
}
