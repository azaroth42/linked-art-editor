import { useMemo } from "react";
import type * as Y from "yjs";

import type { UiProfile, VocabBundle } from "../api/hooks/editing.ts";
import { classificationGroups } from "../widgets/classifications.ts";
import { type WidgetEnv, WidgetEnvContext } from "../widgets/environment.ts";
import { ProfileSectionView } from "./ProfileSectionView.tsx";

interface ProfileRendererProps {
  profile: UiProfile;
  doc: Y.Doc;
  /** `RecordSession.localOrigin`: every write carries it so that undo tracks it. */
  origin: object;
  disabled: boolean;
  bundle: VocabBundle | null;
  /** Labels transcluded on the loaded record's references, by URI. */
  refLabels: ReadonlyMap<string, string>;
}

/**
 * The editing form of a UI Profile (spec §6.1): its `sections` in order, each with its `slots`,
 * every slot drawn by the component the widget registry names for its `widget`. The widgets
 * share the document, the local origin and the Value Set bundle through `WidgetEnvContext`.
 */
export function ProfileRenderer({
  profile,
  doc,
  origin,
  disabled,
  bundle,
  refLabels,
}: ProfileRendererProps) {
  const groups = useMemo(() => classificationGroups(profile, bundle), [profile, bundle]);
  const env = useMemo<WidgetEnv>(
    () => ({
      doc,
      origin,
      disabled,
      recordClass: profile.class,
      bundle,
      refLabels,
      classificationGroups: groups,
    }),
    [doc, origin, disabled, profile.class, bundle, refLabels, groups],
  );
  return (
    <WidgetEnvContext.Provider value={env}>
      <div className="flex w-full max-w-4xl flex-col gap-8" data-testid="profile-form">
        {profile.sections.map((section) => (
          <ProfileSectionView key={section.id} section={section} />
        ))}
      </div>
    </WidgetEnvContext.Provider>
  );
}
