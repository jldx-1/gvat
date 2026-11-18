// Mount CVAT's existing annotation settings without its account/navigation header.
import React from 'react';
import { useDispatch, useSelector } from 'react-redux';
import SettingsModal from 'components/header/settings-modal/settings-modal';
import { switchSettingsModalVisible } from 'actions/settings-actions';
import { registerComponentShortcuts, shortcutsActions } from 'actions/shortcuts-actions';
import GlobalHotKeys from 'utils/mousetrap-react';
import { subKeyMap } from 'utils/component-subkeymap';
import { ShortcutScope } from 'utils/enums';
import { CombinedState } from 'reducers';

const componentShortcuts = {
    SWITCH_SHORTCUTS: {
        name: 'Show shortcuts', description: 'Open/hide the list of available shortcuts',
        sequences: ['f1'], scope: ShortcutScope.GENERAL,
    },
    SWITCH_SETTINGS: {
        name: 'Show settings', description: 'Open/hide settings dialog',
        sequences: ['f2'], scope: ShortcutScope.GENERAL,
    },
};
registerComponentShortcuts(componentShortcuts);

export default function NativeSettings(): JSX.Element {
    const dispatch = useDispatch();
    const visible = useSelector((state: CombinedState) => state.settings.showDialog);
    const shortcuts = useSelector((state: CombinedState) => state.shortcuts);
    const handlers = {
        SWITCH_SETTINGS: (event?: KeyboardEvent) => {
            event?.preventDefault();
            if (!shortcuts.visibleShortcutsHelp) dispatch(switchSettingsModalVisible(!visible));
        },
        SWITCH_SHORTCUTS: (event?: KeyboardEvent) => {
            event?.preventDefault();
            if (!visible) dispatch(shortcutsActions.switchShortcutsModalVisible(!shortcuts.visibleShortcutsHelp));
        },
    };
    return <>
        <GlobalHotKeys keyMap={subKeyMap(componentShortcuts, shortcuts.keyMap)} handlers={handlers} />
        <SettingsModal visible={visible} onClose={() => dispatch(switchSettingsModalVisible(false))} />
    </>;
}
