"""Static check: CS0104-style ambiguous type names in the WPF C# code (no compiler here).

Build break 2026-10-25: `TileMode` was ambiguous between System.Windows.Media.TileMode and UFOS.ai.ModuleTiles.TileMode in
Tiles/TilePreviewView.cs (both namespaces imported with `using`). The brace checker cannot see that. This tool finds, per
file, every simple type name that is used unqualified while TWO OR MORE imported namespaces provide a type of that name:
  - the namespaces of the file's `using` directives plus the project's implicit global usings (ImplicitUsings=enable) and
    `global using` lines of the same project;
  - the types those namespaces provide: our own types (class / struct / record / enum / interface / delegate declarations
    in the scanned C# files) and a curated table of framework types that are known to clash (WPF, IO, Drawing, …);
  - not reported: names declared in the file's own namespace or an enclosing one (they win over usings), names with a
    `using Name = …;` alias, member accesses (`x.Name`), and declarations (`string Name`).
Heuristic, text-based (strings and comments are blanked first). Exit code 1 when something is found.

Usage: python check_cs_ambiguity.py [root]   (default: the UFOS.ai project folder next to HedgeFund)"""
import os
import re
import sys

# Framework types that clash with each other or with ours. Extend when a new clash shows up.
FRAMEWORK = {
    "System": {"Action", "Attribute", "Buffer", "Index", "Range"},
    "System.IO": {"Path", "File", "Directory", "FileInfo", "DirectoryInfo", "Stream"},
    "System.Threading": {"Timer", "Thread", "Monitor", "Timeout", "Lock"},
    "System.Timers": {"Timer"},
    "System.Collections.Generic": {"List`g"},   # `g = generic (List<T>): never clashes with a non-generic List
    "System.Windows": {"Point", "Size", "Rect", "Window", "Thickness", "Visibility", "Application", "MessageBox", "Clipboard",
                       "DataFormats", "FontWeight", "FontStyle", "HorizontalAlignment", "VerticalAlignment", "Style", "Setter",
                       "Trigger", "Condition", "DataTemplate", "ResourceDictionary", "Vector", "Duration", "TextAlignment",
                       "Expression", "PropertyPath", "EventTrigger", "Int32Rect", "SystemColors", "SystemFonts"},
    "System.Windows.Media": {"TileMode", "Brush", "Brushes", "Color", "Colors", "Pen", "Geometry", "Transform", "FontFamily",
                             "Matrix", "Drawing", "ImageSource", "Visual", "VisualTreeHelper", "Stretch", "PathGeometry",
                             "GradientStop", "Typeface", "Fonts", "Effect", "PixelFormat", "PixelFormats", "Region",
                             "BrushMappingMode", "AlignmentX", "AlignmentY", "TextOptions", "RenderOptions", "ToleranceType"},
    "System.Windows.Media.Imaging": {"BitmapImage", "BitmapSource", "BitmapFrame", "BitmapEncoder"},
    "System.Windows.Media.Animation": {"Timeline", "Storyboard", "Animatable", "Clock", "KeyTime", "RepeatBehavior"},
    "System.Windows.Shapes": {"Path", "Rectangle", "Ellipse", "Line", "Polyline", "Polygon", "Shape"},
    "System.Windows.Controls": {"Image", "Button", "Label", "TextBox", "ListBox", "Panel", "Grid", "Border", "Calendar",
                                "ToolTip", "Control", "Orientation", "ContentControl", "Separator", "MenuItem", "ContextMenu",
                                "DataGrid", "Page", "Frame", "ValidationResult", "ItemsControl", "Canvas", "Viewbox", "CheckBox",
                                "ComboBox", "RadioButton", "ProgressBar", "Menu", "TabControl", "Expander", "GroupBox",
                                "Slider", "WebBrowser", "PrintDialog", "Validation", "SelectionMode"},
    "System.Windows.Controls.Primitives": {"Popup", "Selector", "ToggleButton", "Thumb", "Track", "ScrollBar", "StatusBar"},
    "System.Windows.Documents": {"Run", "Hyperlink", "Paragraph", "Table", "List", "Section", "Block", "Inline", "Span",
                                 "Bold", "Italic", "Figure", "Floater", "Underline", "TableRow", "TableCell", "LineBreak"},
    "System.Windows.Input": {"Cursor", "Cursors", "Key", "KeyEventArgs", "MouseEventArgs", "ICommand", "Keyboard", "Mouse",
                             "MouseButton", "MouseButtonEventArgs"},
    "System.Windows.Threading": {"DispatcherTimer", "Dispatcher", "DispatcherPriority"},
    "System.Windows.Data": {"Binding", "IValueConverter", "CollectionView", "BindingMode"},
    "System.Windows.Navigation": {"Journal", "NavigationService"},
    "System.Windows.Automation": {"Condition", "AutomationElement", "AutomationProperties"},
    "System.Drawing": {"Color", "Point", "Size", "Rectangle", "Brush", "Brushes", "Pen", "Image", "Font", "FontFamily", "Icon",
                       "Graphics", "Bitmap", "Region", "Pens"},
    "System.Windows.Forms": {"Timer", "Button", "Label", "Application", "MessageBox", "TextBox", "ListBox", "Panel", "Control",
                             "Cursor", "Cursors", "Image", "Clipboard", "Screen", "Orientation", "Binding", "Padding",
                             "HorizontalAlignment", "Form", "View", "Menu", "MenuItem", "ContextMenu", "ToolTip", "CheckBox",
                             "ComboBox", "RadioButton", "ProgressBar", "TabControl", "GroupBox", "ScrollBar", "Splitter"},
    "System.Text.Json.Nodes": {"JsonNode", "JsonArray", "JsonObject", "JsonValue"},
    "Microsoft.Win32": {"OpenFileDialog", "SaveFileDialog", "Registry"},
}
IMPLICIT = ["System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Net.Http", "System.Threading",
            "System.Threading.Tasks"]
TYPE_PRECEDERS = {"new", "is", "as", "typeof", "sizeof", "default", "return", "throw", "await", "case", "in", "out", "ref",
                  "params", "static", "nameof", "when", "where", "else", "yield", "readonly", "const", "using", "foreach",
                  "public", "private", "protected", "internal", "sealed", "abstract", "virtual", "override", "partial",
                  "async", "unsafe", "volatile", "extern", "required", "init", "get", "set", "var", "class", "struct",
                  "record", "enum", "interface", "delegate", "event", "implicit", "explicit", "operator", "this"}
TYPE_PRECEDERS -= {"class", "struct", "record", "enum", "interface", "delegate", "var"}   # those are followed by a NEW name

_TOKENS = re.compile(r'@\$?"(?:[^"]|"")*"|\$@"(?:[^"]|"")*"|"""[\s\S]*?"""|\$"(?:\{[^{}]*\}|\\.|[^"\\\n])*"|"(?:\\.|[^"\\\n])*"'
                     r"|'(?:\\.|[^'\\\n])'|/\*[\s\S]*?\*/|//[^\n]*")
DECL_RE = re.compile(r"\b(?:class|struct|record|enum|interface)\s+(?:class\s+|struct\s+)?([A-Z][A-Za-z0-9_]*)(\s*<)?")
DELEGATE_RE = re.compile(r"\bdelegate\s+[\w<>\[\],.? ]+?\s+([A-Z][A-Za-z0-9_]*)\s*[<(]")
NS_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)", re.M)
USING_RE = re.compile(r"^\s*(global\s+)?using\s+(static\s+)?([A-Za-z_][\w.]*)\s*(=\s*[^;]+)?;", re.M)


def blank(src):
    return _TOKENS.sub(lambda m: '""' if not m.group(0).startswith("/") else " ", src)


def skip_dir(name):
    return name in ("bin", "obj", "backend", "node_modules", ".venv", ".git", "vendor")


def cs_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for f in filenames:
            if f.endswith(".cs"):
                yield os.path.join(dirpath, f)


def project_of(path, projects):
    best = None
    for proj in projects:
        if path.startswith(proj + os.sep) and (best is None or len(proj) > len(best)):
            best = proj
    return best


def find_projects(root):
    """{project dir: implicit usings enabled} for every .csproj under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for f in filenames:
            if f.endswith(".csproj"):
                text = open(os.path.join(dirpath, f), encoding="utf-8-sig", errors="replace").read()
                out[dirpath] = bool(re.search(r"<ImplicitUsings>\s*enable\s*</ImplicitUsings>", text, re.I))
    return out


def analyse(files_src, projects=None):
    """files_src: {path: source}. Returns findings [(path, line, name, [namespaces])]."""
    projects = projects or {}
    parsed = {}
    ours = {}            # namespace -> set(type names)
    global_usings = {}   # project -> set(namespaces)
    for path, src in files_src.items():
        code = blank(src)
        nss = NS_RE.findall(code)
        ns = nss[0] if nss else ""
        names = {n + ("`g" if g else "") for n, g in DECL_RE.findall(code)} | set(DELEGATE_RE.findall(code))
        ours.setdefault(ns, set()).update(names)
        usings, aliases = set(), set()
        proj = project_of(path, projects)
        for g, static, target, alias in USING_RE.findall(code):
            if alias:
                aliases.add(target)
            elif not static:
                (global_usings.setdefault(proj, set()) if g else usings).add(target)
        parsed[path] = (code, ns, usings, aliases, proj)

    findings = []
    for path, (code, ns, usings, aliases, proj) in parsed.items():
        imported = set(usings) | global_usings.get(proj, set())
        if proj is not None and projects.get(proj):
            imported |= set(IMPLICIT)
        # names visible through the namespace chain win over usings
        chain = set()
        parts = ns.split(".") if ns else []
        for i in range(len(parts), 0, -1):
            chain |= ours.get(".".join(parts[:i]), set())
        providers = {}
        for n in imported:
            for t in FRAMEWORK.get(n, ()):
                providers.setdefault(t, set()).add(n)
            for t in ours.get(n, ()):
                providers.setdefault(t, set()).add(n)
        candidates = {t: sorted(s) for t, s in providers.items()
                      if len(s) > 1 and t.replace("`g", "") not in aliases and t not in chain}
        if not candidates:
            continue
        plain = {t.replace("`g", "") for t in candidates}
        pattern = re.compile(r"(?<![\w.])(" + "|".join(sorted(map(re.escape, plain), key=len, reverse=True)) + r")\b")
        for m in pattern.finditer(code):
            before = code[:m.start()].rstrip()
            prev = re.search(r"([A-Za-z_]\w*)$", before)
            if prev and prev.group(1) not in TYPE_PRECEDERS:
                continue   # a declaration: `string Name`
            after = code[m.end():m.end() + 3].lstrip()
            key = m.group(1) + ("`g" if after.startswith("<") else "")
            if key not in candidates:
                continue   # e.g. List<T> (generic) vs a non-generic List
            if after.startswith(":") and not after.startswith("::") or (after.startswith("=") and not after.startswith("==") and not after.startswith("=>")):
                continue   # named argument / initializer member / assignment target
            line = code.count("\n", 0, m.start()) + 1
            findings.append((path, line, m.group(1), candidates[key]))
    return findings


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    root = argv[1] if len(argv) > 1 else os.path.normpath(os.path.join(here, "..", ".."))
    files = {p: open(p, encoding="utf-8-sig", errors="replace").read() for p in cs_files(root)}
    findings = analyse(files, find_projects(root))
    seen = set()
    for path, line, name, nss in findings:
        key = (path, name)
        if key in seen:
            continue
        seen.add(key)
        print(f"{os.path.relpath(path, root)}:{line}: '{name}' is ambiguous between {', '.join(nss)} "
              f"(add `using {name} = <namespace>.{name};` or qualify it)")
    if seen:
        print(f"FOUND {len(seen)} ambiguous type name use(s) in {len(files)} files")
        return 1
    print(f"OK - no ambiguous type names in {len(files)} C# files under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
