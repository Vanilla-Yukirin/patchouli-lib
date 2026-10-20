import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { useEffect, useRef, useState } from "react";
import type { FileEntry } from "./api";
import { renderMarkdown, titleFromMarkdown } from "./markdown";
import { usePreferences } from "./preferences";
export function Markdown({
	source,
	files = [],
	guide = false,
	sourceFile,
}: {
	source: string;
	files?: FileEntry[];
	guide?: boolean;
	sourceFile?: string;
}) {
	return (
		<article
			className="prose"
			dangerouslySetInnerHTML={{
				__html: renderMarkdown(source, files, guide, sourceFile),
			}}
		/>
	);
}
export function SourceEditor({
	value,
	onChange,
	nonce,
	preview = true,
	files = [],
	readOnly = false,
	sourceFile,
}: {
	value: string;
	onChange: (value: string) => void;
	nonce: string;
	preview?: boolean;
	files?: FileEntry[];
	readOnly?: boolean;
	sourceFile?: string;
}) {
	const host = useRef<HTMLDivElement>(null);
	const editor = useRef<EditorView | null>(null);
	const callback = useRef(onChange);
	const syncing = useRef(false);
	callback.current = onChange;
	const { t } = usePreferences();
	const [tab, setTab] = useState("edit");
	useEffect(() => {
		if (!host.current) return;
		const view = new EditorView({
			parent: host.current,
			state: EditorState.create({
				doc: value,
				extensions: [
					EditorState.readOnly.of(readOnly),
					EditorView.editable.of(!readOnly),
					history(),
					keymap.of([...defaultKeymap, ...historyKeymap]),
					markdown(),
					EditorView.lineWrapping,
					EditorView.cspNonce.of(nonce),
					EditorView.updateListener.of((update) => {
						if (update.docChanged && !syncing.current)
							callback.current(update.state.doc.toString());
					}),
				],
			}),
		});
		editor.current = view;
		return () => {
			view.destroy();
			editor.current = null;
		};
	}, [nonce, readOnly]);
	useEffect(() => {
		const view = editor.current;
		if (view && view.state.doc.toString() !== value) {
			syncing.current = true;
			view.dispatch({
				changes: { from: 0, to: view.state.doc.length, insert: value },
			});
			syncing.current = false;
		}
	}, [value]);
	return (
		<div className={`source-workspace mobile-${tab}`}>
			<div className="mobile-editor-tabs">
				<button type="button" onClick={() => setTab("edit")}>
					{t("editor.edit")}
				</button>
				<button type="button" onClick={() => setTab("preview")}>
					{t("editor.preview")}
				</button>
			</div>
			<div className="source-pane" ref={host} />
			{preview && (
				<div className="preview-pane">
					<p className="muted">
						# {titleFromMarkdown(value) || t("directory.untitled")}
					</p>
					<Markdown source={value} files={files} sourceFile={sourceFile} />
				</div>
			)}
		</div>
	);
}
