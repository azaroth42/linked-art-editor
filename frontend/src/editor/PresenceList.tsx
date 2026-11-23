import { useTranslation } from "react-i18next";

import type { Peer } from "../crdt/useRecordDoc.ts";

/** Who else has this draft open (one entry per connection, as the server stamped it). */
export function PresenceList({ peers }: { peers: Peer[] }) {
  const { t } = useTranslation("editor");
  if (peers.length === 0) {
    return (
      <p className="text-slate-700" data-testid="presence">
        {t("presence.alone")}
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="presence">
      <span className="text-slate-700">{t("presence.label")}</span>
      <ul className="flex flex-wrap gap-2">
        {peers.map((peer) => {
          const name = peer.displayName ?? t("presence.unknown");
          return (
            <li
              key={peer.clientId}
              className="rounded-full bg-slate-200 px-3 py-0.5 text-sm text-slate-900"
            >
              {peer.readOnly ? t("presence.read_only", { name }) : name}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
