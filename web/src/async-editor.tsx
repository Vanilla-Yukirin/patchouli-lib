import { type ComponentProps, lazy, Suspense } from "react";
import { Loading } from "./ui";

const Editor = lazy(() =>
	import("./editor").then((module) => ({ default: module.SourceEditor })),
);
export function SourceEditor(
	props: ComponentProps<typeof import("./editor").SourceEditor>,
) {
	return (
		<Suspense fallback={<Loading />}>
			<Editor {...props} />
		</Suspense>
	);
}
