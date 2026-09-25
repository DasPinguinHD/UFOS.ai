# Copilot Instructions

## Projektrichtlinien
- **Vor der Umsetzung interviewen (Regel des Nutzers, 2026-09-25):** Vor jeder nicht-trivialen Umsetzung den Nutzer zur Umsetzung und Struktur befragen, um Schwachstellen im Entwurf, vergessene Aspekte (Randfälle, Datenhaltung, Fehlerzustände, KI-Kennzeichnung, Kosten, Performance, UX-Zustände) und Fehlkonstruktionen vor dem ersten Code zu finden. Konkrete Fragen mit Optionen und Empfehlung stellen, dann den zusammengeführten Plan nennen und umsetzen. Ausnahme: triviale, eindeutige Änderungen oder ausdrücklicher Wunsch, ohne Rückfragen fortzufahren.
- **Sub-Agents erlaubt (Freigabe des Nutzers, 2026-09-25):** KI-Assistenten dürfen nach eigenem Ermessen Sub-Agents einsetzen, z. B. für parallele, voneinander unabhängige Arbeit, breite Code-Suchen oder ein unabhängiges Review. Für Sub-Agents gelten dieselben Regeln (keine `.env`-Inhalte, keine Commits/Pushes ohne Auftrag, im angefragten Umfang bleiben); ihre Ergebnisse vor der Rückmeldung selbst prüfen.
- **Dies ist ein Proof-of-Concept-Portfolio-Projekt, kein veröffentlichtes Produkt.** Nicht für echten Handel/Finanzentscheidungen oder kommerzielle Nutzung gedacht — Tonalität in READMEs, In-App-Texten und Kommentaren entsprechend halten, keine Produktionsreife suggerieren.
- Bei UI-Redesigns soll bestehender dynamisch generierter (code-behind) Fenster-Stil exakt wiederverwendet/übernommen werden, nicht neu/generisch gestaltet.
- Kommentare im Code sollen in Deutsch verfasst werden, um die Verständlichkeit für das Team zu gewährleisten.
- Text, Nachrichten und Meldungen sollen in Englisch verfasst werden, bis eine mehrsprachige Lösung implementiert ist.
- Die Projektstruktur für ein neues Modul ist wie folgt zu gestalten:
	- MODULNAME
		- tools/
		- backend/
		- requirements.txt
		- README.md
		- Dockerfile
		- .env.example
	- Für jedes neue Modul ist eine Integration in den globalen Error Handler und Logger zu erstellen. Hierfür dienen die bestehenden Module als Vorbild.
- **KI-Kennzeichnung (EU AI Act Art. 50) ist Pflicht für jede KI-Ausgabe**, insbesondere für KI-Analysen per Knopfdruck ohne manuelle Nutzereingabe: Button mit `✦` + KI-Tooltip, sichtbares Badge `✦ AI-GENERATED · <Modell> · <Uhrzeit>` vor dem Text (schon im Ladezustand), Hinweiszeile danach, eigener violetter Container (`AiAccentBrush`), maschinenlesbares `provenance`-Objekt in jeder Backend-Antwort mit LLM-Text, Screenreader-Text beginnt mit "AI-generated", Fehler nie als Analyse tarnen. Texte/Helfer zentral in `UFOS.ai/Shared/AiContent.cs`; Details in `CLAUDE.md`. Der Start-Hinweis (`UFOS.ai/Shared/AiDisclaimerWindow.cs`) darf nicht entfernt oder abgeschwächt werden.
- Neu erstellte Modulfenster sollen standardmäßig das LiveStreamAgent-Design übernehmen, insbesondere das rahmenlose dunkle Standardlayout mit den exakten Minimize-/Close-Buttons aus UFOS.ai/MainWindow und vergleichbaren Modulfenstern.
- **Rahmenlose Fenster müssen Vollbild korrekt darstellen.** Jedes Fenster mit `WindowStyle="None"` braucht einen `StateChanged`-Handler: Maximiert hängt der unsichtbare Größenänderungsrahmen über den Bildschirmrand und schneidet Titelleiste und obere Ecken ab. Im maximierten Zustand den Root-Border um `SystemParameters.WindowResizeBorderThickness` + 4 einrücken und die Eckenradien auf 0 setzen, sonst zurücksetzen. Referenz: `Window_StateChanged` in `HedgeFund/HedgeFundWindow.xaml.cs` bzw. `HedgeFund/Newsroom/FinancialNewsroomWindow.xaml.cs`. Bei jedem neuen Fenster den Vollbildmodus prüfen. Außerdem muss das Fenster **aus dem Vollbild herausgezogen werden können**: `DragMove()` wirkt bei maximierten Fenstern nicht, daher braucht die Titelleiste `MouseLeftButtonDown`/`MouseMove`/`MouseLeftButtonUp`-Handler, die das Fenster erst bei tatsächlicher Mausbewegung mit gedrückter Taste wiederherstellen (einfacher Klick tut nichts), den Cursor an seiner relativen Position auf der Titelleiste halten und dann `DragMove()` aufrufen (Referenz: `TitleBar_MouseMove` in denselben Dateien). Test: maximieren → Titelleiste ziehen → Fenster wird kleiner und folgt der Maus.
