# Kodi add-on runtime facts

Facts about Kodi's Python API and add-on runtime for reviewing repo-scripts submissions. `verified` facts cite Kodi source as path[:line]@ref (refs: 18.9-Leia, 19.0-Matrix, 19.5-Matrix, 20.5-Nexus, 21.3-Omega, Piers). `unverified` facts come from add-on authors' experience or fixed review findings; word a finding that rests on one as a likely problem and show it in the add-on's own code.

## Abort, services and threads

- verified: `xbmc.Monitor().waitForAbort(t)` returns True when Kodi asks the add-on to stop, False on timeout, and wakes every 100 ms to run pending callbacks. A timeout of 0 or less waits forever. | check: retry and sleep loops break when it returns True | at: xbmc/interfaces/legacy/Monitor.cpp:42-59@Piers
- verified: Monitor callbacks (`onNotification`, `onSettingsChanged`) are dispatched by the pending-call queue that `waitForAbort` runs between waits. `xbmc.sleep` runs them too, between 100 ms slices, but never wakes early on abort. | check: a service that sleeps with `time.sleep` gets its callbacks late or never | at: xbmc/interfaces/legacy/Monitor.cpp:57-58@Piers, xbmc/interfaces/legacy/ModuleXbmc.cpp:sleep@19.5-Matrix, @Piers
- verified: when Kodi stops a script (exit, disable, update) it fires the Monitor abort, waits 5 s for the script to finish, then sets SystemExit as the pending exception of every Python thread the script started. A thread blocked in a C call (socket read, `time.sleep`, `xbmc.sleep`) sees it only when that call returns, and a bare `except:` or `except BaseException` swallows it. Kodi's exit then waits with no limit for every thread to end, daemon or not. | check: the longest blocking call a thread can be in at the abort sets the exit delay, not the number of queued items; a loop that swallows SystemExit and keeps going holds Kodi's exit forever; queued work is saved before exit | at: xbmc/interfaces/python/PythonInvoker.cpp:63, 404-428, 511-520, 557-566@Piers, the same in PythonInvoker.cpp:stop@19.5-Matrix, @20.5-Nexus, @21.3-Omega; xbmc/interfaces/generic/ScriptInvocationManager.cpp:84-88@Piers; disable and update stop it the same way through xbmc/addons/Service.cpp:43-51, 124-128@Piers
- verified: `xbmc.getInfoLabel` and `getInfoImage` evaluate on the calling thread without the GUI lock; `getCondVisibility` takes it, and JSON-RPC `XBMC.GetInfoLabels` runs on Kodi's main thread. | check: a `ListItem.*` or `Container.*` read from a service or callback thread can crash Kodi when it overlaps the GUI thread changing that item; word it as a possible crash and say how often the read runs and while what is open | at: xbmc/interfaces/legacy/ModuleXbmc.cpp:267-290, 344-352@Piers, the same at ModuleXbmc.cpp:getInfoLabel, getCondVisibility@19.5-Matrix, @20.5-Nexus, @21.3-Omega; xbmc/interfaces/json-rpc/XBMCOperations.cpp:33@Piers, :34@19.5-Matrix
- unverified: a settings save reloads only the first `xbmcaddon.Addon()` instance created; a long-running service that keeps one instance reads stale settings unless it builds a fresh one in `onSettingsChanged`.
- unverified: without `reuselanguageinvoker`, every plugin or script call starts a fresh interpreter, so module-level state is lost between calls and heavy top-level imports cost every call. With it, module state survives between calls of the same plugin until another script starts in that one shared slot.

## Python API behavior

- verified: `print()` reaches kodi.log at LOGDEBUG, stderr at LOGERROR. | at: xbmc/interfaces/python/AddonPythonInvoker.cpp:18-33@Piers
- verified: `xbmcaddon.Addon()` without an id raises "No valid addon id could be obtained" when the code was not started as the add-on, such as `RunScript` by file path. | check: code reachable through RunScript by path passes the id | at: xbmc/interfaces/legacy/Addon.cpp:61-67@Piers
- verified: `Addon().getLocalizedString(N)` reads the add-on's own strings in any id range and returns an empty string for a missing id; `xbmc.getLocalizedString(N)` and numeric labels in a script window's XML read Kodi's strings, except that from Piers on ids 31000-31999 read the skin's; `$ADDON[<id> N]` reads the named add-on's file. | at: xbmc/resources/LocalizeStrings.cpp:214-224@Piers
- verified: `xbmcvfs.exists` treats a path ending in a slash as a folder and anything else as a file, so a folder path without the slash usually reports False. | at: xbmc/interfaces/legacy/ModuleXbmcvfs.cpp:exists@Piers
- verified: `ListItem.setInfo` and the singular setters are deprecated from Nexus in favor of the InfoTag setters (`getVideoInfoTag().setTitle(...)`) and log a deprecation warning per call. On Matrix only `setInfo` exists. | at: xbmc/interfaces/legacy/ListItem.h:784@Piers
- verified: `InfoTagVideo.setIMDBNumber` writes the current default unique id slot, so `setUniqueIDs({...}, "tmdb")` followed by `setIMDBNumber("tt...")` replaces the tmdb id. | at: xbmc/interfaces/legacy/InfoTagVideo.cpp:setIMDBNumberRaw@Piers
- verified: `Player.getVideoInfoTag()` raises unless video is playing; playback can stop between `isPlayingVideo()` and the read. | check: the read is guarded or the exception handled | at: xbmc/interfaces/legacy/Player.cpp:getVideoInfoTag@Piers
- verified: Kodi reads the `inputstream` ListItem property; `inputstreamaddon` still works but logs a deprecation on every play. | at: xbmc/cores/VideoPlayer/DVDInputStreams/InputStreamAddon.cpp:87-92@Piers
- verified: builtins run through `xbmc.executebuiltin` are passed verbatim; `$INFO[...]` in their arguments is not expanded. | at: xbmc/application/Application.cpp:ExecuteXBMCAction@Piers
- verified: WindowXML and WindowXMLDialog load the XML from the current skin first, then the add-on's `resources/skins/<skin id>`, then its `Default` folder, so a skin file of the same name restyles the add-on's window. | at: xbmc/interfaces/legacy/WindowXML.cpp:WindowXML@Piers
- verified: `WindowXMLDialog.show()` returns at once and the dialog still takes input, since WindowXMLDialog reports itself modal; `doModal()` only adds the wait. | at: xbmc/guilib/GUIWindowManager.cpp:HandleAction@Piers, xbmc/interfaces/legacy/WindowXML.h@Piers
- verified: action ids: 9 is ACTION_PARENT_DIR, 10 is ACTION_PREVIOUS_MENU, 92 is ACTION_NAV_BACK. | at: xbmc/input/actions/ActionIDs.h@Piers
- unverified: control ids 13000-13099 in a Python dialog's XML can equal the dialog's own window id and crash Kodi with a stack overflow.
- unverified: `ListItem(offscreen=True)` suits items built before they reach a container or `Dialog().select()`; not items read back with `getListItem` and edited in place.
- unverified: `Player().getVideoInfoTag()` polled in a loop retains memory per call.
- unverified: `datetime.strptime` can raise `TypeError: 'NoneType' object is not callable` after its first use in a Kodi session; `time.strptime` or `fromisoformat` avoid it.

## JSON-RPC

- verified: parameters are checked against the schema; one unknown property in a `properties` list fails the whole call with InvalidParams. A field added on a newer Kodi fails the call on an older one. | at: xbmc/interfaces/json-rpc/JSONServiceDescription.cpp:687-850@Piers
- unverified: code that reads `result` without checking for `error` crashes, or advances a sync checkpoint past a failed write.
- unverified: playback updates the play count and resume point through the same notifications as a manual change, so a sync add-on must tell its own writes apart. `userrating` takes an int from 0 to 10.

## Plugins and context items

- unverified: a subtitle add-on's download action must end the directory on HTTP errors and expired links, not raise.
- verified: an add-on's `<news>` is what users see before installing; `changelog.txt` is read only when `<news>` is empty, for installed add-ons. | at: xbmc/addons/addoninfo/AddonInfoBuilder.cpp:563-583@Piers

## Python versions

- verified: Windows builds bundle Python 3.8.15 on Matrix, Nexus and Omega, so code for those branches runs on 3.8. Kodi's own Piers builds (Windows, Android, macOS, Linux through depends) bundle 3.14, as does LibreELEC; Linux distro builds of Piers use the distro's Python, so Piers code can meet an older one. | at: project/BuildDependencies/scripts/0_package.target-win10-x64.list@19.5-Matrix, @20.5-Nexus, @21.3-Omega; tools/depends/target/python3/PYTHON3-VERSION@Piers
- unverified: Python's sqlite3 on some Kodi platforms is SQLite 3.24 to 3.31: at most 999 bound parameters, no `RETURNING`, no `DROP COLUMN`.
- unverified: a `requests` timeout limits only inactivity between bytes, not the total time; a call with no timeout can block forever and hold up Kodi's exit.

## Secrets and privacy

- unverified: users post debug logs publicly; tokens (including `#access_token=` in URLs), credentials and search queries must not reach the log at any level.
- unverified: an app's client id or a public OAuth client secret shipped in the code is not a finding; a personal or paid API key, or a secret with no OAuth flow, is. Repo reviewers ask for keys to be obfuscated, which the add-on check reports.

## Not findings

- an export or file read the user starts from a dialog, to a folder the user picks: the user's action is the consent
- crash or usage reporting that asks the user before each send
- fragile scraping of a site that works today
- a broad `except` or an MD5 cache key that never reaches the user
- asking for a None check the code already has; edge cases the author showed unreachable
- behavior the add-on's own README, settings help or news describes as intended, presented as a bug, unless it crashes, loses data, opens a security hole or hangs at shutdown
- in a new-style settings `dependency`, `operator="!is"` with an empty value means "is not empty"
- the "left several classes in memory" log line at exit, unless the class it names is `xbmcaddon.Addon` in a service that reads settings through a long-lived instance (see the settings reload fact above)
- a fresh `xbmc.Player()` per use; `xbmcvfs.File` without try/finally on a read that cannot raise midway
- orphaned values in the user's settings file from settings an older version had
