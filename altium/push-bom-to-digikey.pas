{ push-bom-to-digikey.pas
  Emits a BOM CSV from the focused Altium project suitable for direct
  consumption by digikey_push.py.

  How to invoke
  -------------
  1. Open the .PrjPcb in Altium (project must be focused / active).
  2. File > Scripts > Open Script Project... >
       <repo>\altium\push-bom-to-digikey.PrjScr
  3. In the Scripts panel right-click EmitDigiKeyBOM > Run.

  Output
  ------
  <project-dir>\digikey-push.csv   -- ready for digikey_push.py

  Column order (matches digikey_push.py auto-detection):
    Designator, Comment, Manufacturer 1, Manufacturer Part Number 1,
    Supplier 1, Supplier Part Number 1, Quantity

  The Python side ignores the Comment column; it is present so the CSV is
  human-readable on its own and matches the CubePilot example-bom.csv shape.

  DNP / filter rules
  ------------------
  A component row is skipped if any of:
    - MPN column is empty after applying the fallback chain.
    - Component's current project variant marks it eVariation_NotFitted.
    - Component.GetState_ComponentKind ordinal is 5 (Standard No-BOM).
      Ordinal 5 is undocumented but stable on AD26 -- bench-verified against
      the CubeRacer project (InteractiveHTMLBOM4Altium2.pas:1787, 2026-05-23).

  Aggregation
  -----------
  Components sharing the same MPN (case-insensitive) are merged into one row:
    - Designators concatenated as "R1, R2, R3".
    - Quantity summed.
    - First non-empty DKPN wins (mirrors aggregate_by_mpn in digikey_push.py).
    - First non-empty Comment wins.

  API provenance
  --------------
  GetWorkspace / IWorkspace / DM_FocusedProject / DM_ProjectFullPath:
    [verified-on-AD26, altium-emit-review-pack.pas -- bench-confirmed 2026-05-14]
  CurrProject.DM_DocumentFlattened -> IDocument:
    [pattern, InteractiveHTMLBOM4Altium2.pas:326 -- not yet bench-verified on AD26]
  FlattenedDoc.DM_ComponentCount, DM_Components[i] -> IComponent:
    [pattern, InteractiveHTMLBOM4Altium2.pas:374,381]
  IComponent.DM_ParameterCount, DM_Parameters(i) -> IParameter:
    [pattern, InteractiveHTMLBOM4Altium2.pas:382,385]
  IParameter.DM_Name, IParameter.DM_Value:
    [pattern, InteractiveHTMLBOM4Altium2.pas:386,563]
  IComponent.SourceDesignator:
    [pattern, InteractiveHTMLBOM4Altium2.pas:878]
  Ord(Component.GetState_ComponentKind) = 5 (NoBOM detection):
    [pattern, InteractiveHTMLBOM4Altium2.pas:1787]
  IProjectVariant / DM_CurrentProjectVariant / DM_FindComponentVariationByDesignator
    / IComponentVariation.DM_VariationKind / eVariation_NotFitted:
    [pattern, InteractiveHTMLBOM4Altium2.pas:2733,476-495]
  CurrProject.DM_ProjectFileName (filename without path):
    [pattern, InteractiveHTMLBOM4Altium2.pas:84,573]
  ProjectDirFromPath (manual char-walk):
    [verified-on-AD26, altium-emit-review-pack.pas:70-80]
  TStringList.Add / SaveToFile:
    [verified-on-AD26, hello-sentinel.pas 2026-05-14]
  DateTimeToStr(Now):
    [verified-on-AD26, altium-emit-review-pack.pas]
  FileExists:
    [pattern, altium-emit-review-pack.pas:147]

  Unverified on AD26 (probe required before relying on these):
    DM_DocumentFlattened, DM_ComponentCount, DM_Components[i],
    DM_ParameterCount, DM_Parameters(i), DM_Name, DM_Value,
    SourceDesignator, GetState_ComponentKind ordinal 5,
    DM_CurrentProjectVariant, DM_FindComponentVariationByDesignator,
    DM_VariationKind, eVariation_NotFitted, DM_ProjectFileName.
  See probe file: push-bom-to-digikey-probe.pas

  NOTE: X2.exe -RunScript is dead on AD26. Do not try to automate this
  from a shell script. The Scripts panel is the only working trigger.
}

// ---------------------------------------------------------------------------
// Helpers -- defined before first use (AD26 is single-pass, no forward refs).
// ---------------------------------------------------------------------------

{ ProjectDirFromPath: derive '<dir>\' from a full file path.
  Manual char-walk; ExtractFilePath unverified on AD26.
  Pattern from altium-emit-review-pack.pas:70-80 [verified-on-AD26] }
function ProjectDirFromPath(full : String) : String;
var
  i : Integer;
begin
  i := Length(full);
  while (i > 0) and (full[i] <> '\') do i := i - 1;
  if i = 0 then
    Result := 'C:\Users\Public\'
  else
    Result := Copy(full, 1, i);
end;

{ ProjectNameFromPath: derive bare project name (no extension, no dir).
  Strips trailing .PrjPcb or .PrjScr etc. and directory prefix.
  Pattern from InteractiveHTMLBOM4Altium2.pas:573 [not bench-verified on AD26] }
function ProjectNameFromPath(full : String) : String;
var
  i     : Integer;
  fname : String;
  dot   : Integer;
begin
  // strip directory
  i := Length(full);
  while (i > 0) and (full[i] <> '\') do i := i - 1;
  if i = 0 then
    fname := full
  else
    fname := Copy(full, i + 1, Length(full) - i);
  // strip extension
  dot := 0;
  for i := Length(fname) downto 1 do
  begin
    if fname[i] = '.' then
    begin
      dot := i;
      Break;
    end;
  end;
  if dot > 1 then
    Result := Copy(fname, 1, dot - 1)
  else
    Result := fname;
end;

{ CSVQuote: wrap value in double-quotes, escaping embedded double-quotes.
  RFC 4180 quoting -- plain Pascal, no RTL dependency. }
function CSVQuote(s : String) : String;
var
  i   : Integer;
  out : String;
begin
  out := '"';
  for i := 1 to Length(s) do
  begin
    if s[i] = '"' then
      out := out + '""'
    else
      out := out + s[i];
  end;
  out := out + '"';
  Result := out;
end;

{ UpperCaseStr: upper-case a string for case-insensitive MPN key.
  Plain Pascal loop; no Delphi RTL dependency beyond Ord/Chr. }
function UpperCaseStr(s : String) : String;
var
  i   : Integer;
  c   : Char;
  out : String;
begin
  out := '';
  for i := 1 to Length(s) do
  begin
    c := s[i];
    if (Ord(c) >= 97) and (Ord(c) <= 122) then
      out := out + Chr(Ord(c) - 32)
    else
      out := out + c;
  end;
  Result := out;
end;

{ TrimStr: strip leading/trailing spaces.
  Plain Pascal loop. }
function TrimStr(s : String) : String;
var
  lo, hi : Integer;
begin
  lo := 1;
  hi := Length(s);
  while (lo <= hi) and (s[lo] = ' ') do lo := lo + 1;
  while (hi >= lo) and (s[hi] = ' ') do hi := hi - 1;
  if lo > hi then
    Result := ''
  else
    Result := Copy(s, lo, hi - lo + 1);
end;

{ FindParam: walk a component's parameter list and return the value of the
  first parameter whose name matches any entry in a comma-delimited
  candidate list (case-insensitive). Returns '' on no match.
  [pattern, InteractiveHTMLBOM4Altium2.pas:382-386] -- unverified on AD26. }
function FindParam(Comp : IComponent; names : String) : String;
var
  pi    : Integer;
  parm  : IParameter;
  nup   : String;
  cand  : String;
  sep   : Integer;
  token : String;
  rest  : String;
begin
  Result := '';
  for pi := 0 to Comp.DM_ParameterCount - 1 do
  begin
    parm := Comp.DM_Parameters(pi);
    nup  := UpperCaseStr(TrimStr(parm.DM_Name));
    // walk the comma-delimited candidate list
    rest := names;
    while rest <> '' do
    begin
      sep := Pos(',', rest);
      if sep = 0 then
      begin
        token := rest;
        rest  := '';
      end
      else
      begin
        token := Copy(rest, 1, sep - 1);
        rest  := Copy(rest, sep + 1, Length(rest) - sep);
      end;
      cand := UpperCaseStr(TrimStr(token));
      if nup = cand then
      begin
        Result := TrimStr(parm.DM_Value);
        Exit;
      end;
    end;
  end;
end;

// ---------------------------------------------------------------------------
// Aggregation scratch arrays
// No dynamic arrays in AD26 DelphiScript -- use parallel TStringLists with
// a synthetic index. Each list holds one field per unique MPN (keyed by the
// upper-cased MPN). MPNKeys tracks insertion order for deterministic output.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// ENTRY POINT
// ---------------------------------------------------------------------------
procedure EmitDigiKeyBOM;
var
  Workspace   : IWorkspace;
  Project     : IProject;
  Variant     : IProjectVariant;
  FlatDoc     : IDocument;
  Comp        : IComponent;
  CompVar     : IComponentVariation;

  ProjectPath : String;
  ProjectName : String;
  CsvPath     : String;

  ci          : Integer;
  CompCount   : Integer;

  Desig       : String;
  Comment     : String;
  Mfr1        : String;
  Mpn1        : String;
  Sup1        : String;
  Spn1        : String;
  MpnKey      : String;
  IsFitted    : Boolean;
  IsNoBOM     : Boolean;

  // Parallel TStringLists acting as an ordered map: MPN -> fields.
  // Index in MpnKeys is the row number; all other lists share that index.
  MpnKeys     : TStringList; // upper-cased MPN, for lookup
  MpnOrig     : TStringList; // original-case MPN (first seen)
  AccDesig    : TStringList; // accumulated "R1, R2, R3"
  AccComment  : TStringList; // first non-empty comment
  AccMfr1     : TStringList;
  AccSup1     : TStringList;
  AccSpn1     : TStringList; // first non-empty DKPN
  AccQty      : TStringList; // integer as string

  idx         : Integer;
  existing    : String;
  newQty      : Integer;
  oldQty      : Integer;

  Lines       : TStringList;
  RowCount    : Integer;
  TotalQty    : Integer;
  ri          : Integer;
  Msg         : String;

begin
  ShowMessage('EmitDigiKeyBOM: starting.');

  // ------------------------------------------------------------------
  // 1. Acquire workspace + focused project
  //    [verified-on-AD26, altium-emit-review-pack.pas]
  // ------------------------------------------------------------------
  Workspace := GetWorkspace;
  if Workspace = nil then
  begin
    ShowMessage('GetWorkspace returned nil. Is Altium fully loaded?');
    Exit;
  end;

  Project := Workspace.DM_FocusedProject;
  if Project = nil then
  begin
    ShowMessage('No focused project.' + #13#10 + #13#10 +
                'Open a .PrjPcb in Altium, then re-run this script.');
    Exit;
  end;

  ProjectPath := ProjectDirFromPath(Project.DM_ProjectFullPath);
  ProjectName := ProjectNameFromPath(Project.DM_ProjectFullPath);
  CsvPath     := ProjectPath + 'digikey-push.csv';

  ShowMessage('Project: ' + ProjectName + #13#10 +
              'Output:  ' + CsvPath);

  // ------------------------------------------------------------------
  // 2. Get the current variant (nil = no variant / base design).
  //    [pattern, InteractiveHTMLBOM4Altium2.pas:2733] -- unverified on AD26.
  // ------------------------------------------------------------------
  Variant := Project.DM_CurrentProjectVariant;
  // nil is fine -- means no variant selected or project has no variants.

  // ------------------------------------------------------------------
  // 3. Get the flattened document.
  //    If DM_DocumentFlattened returns nil, try compiling the project first.
  //    [pattern, InteractiveHTMLBOM4Altium2.pas:324-337] -- unverified on AD26.
  // ------------------------------------------------------------------
  FlatDoc := Project.DM_DocumentFlattened;
  if FlatDoc = nil then
  begin
    ShowMessage('Flattened document is nil -- attempting project compile...');
    ResetParameters;
    AddStringParameter('ObjectKind', 'Project');
    RunProcess('WorkspaceManager:Compile');
    FlatDoc := Project.DM_DocumentFlattened;
  end;

  if FlatDoc = nil then
  begin
    ShowMessage('Could not obtain a flattened document.' + #13#10 + #13#10 +
                'Compile the project in Altium (Project > Compile PCB Project)' + #13#10 +
                'then re-run this script.');
    Exit;
  end;

  CompCount := FlatDoc.DM_ComponentCount;
  if CompCount = 0 then
  begin
    ShowMessage('Flattened document has 0 components. Has the project compiled?');
    Exit;
  end;

  ShowMessage('Components in flattened document: ' + IntToStr(CompCount));

  // ------------------------------------------------------------------
  // 4. Iterate components and accumulate into parallel lists.
  // ------------------------------------------------------------------
  MpnKeys  := TStringList.Create;
  MpnOrig  := TStringList.Create;
  AccDesig := TStringList.Create;
  AccComment := TStringList.Create;
  AccMfr1  := TStringList.Create;
  AccSup1  := TStringList.Create;
  AccSpn1  := TStringList.Create;
  AccQty   := TStringList.Create;

  for ci := 0 to CompCount - 1 do
  begin
    Comp := FlatDoc.DM_Components[ci];
    if Comp = nil then Continue;

    Desig := TrimStr(Comp.SourceDesignator);

    // -- NoBOM check (ordinal 5 = Standard No-BOM on AD26) --
    // [pattern, InteractiveHTMLBOM4Altium2.pas:1787] -- unverified on AD26.
    IsNoBOM := (Ord(Comp.GetState_ComponentKind) = 5);
    if IsNoBOM then Continue;

    // -- DNP / variant check --
    // [pattern, InteractiveHTMLBOM4Altium2.pas:452-512] -- unverified on AD26.
    IsFitted := True;
    if Variant <> nil then
    begin
      CompVar := Variant.DM_FindComponentVariationByDesignator(Desig);
      if CompVar <> nil then
      begin
        if CompVar.DM_VariationKind = eVariation_NotFitted then
          IsFitted := False;
      end
      else
      begin
        // No variation defined for this designator -- include it.
        IsFitted := True;
      end;
    end;
    if not IsFitted then Continue;

    // -- Extract parameters --
    // MPN fallback chain mirrors digikey_push.py DEFAULT_MPN_COLS order.
    Mpn1 := FindParam(Comp,
      'Manufacturer Part Number 1,Manufacturer Part Number,MPN,Part Number,PartNumber,ManufacturerPartNumber');

    // Skip rows with no MPN (undetermined / mechanical).
    if Mpn1 = '' then Continue;

    Mfr1 := FindParam(Comp, 'Manufacturer 1,Manufacturer');
    Sup1 := FindParam(Comp, 'Supplier 1,Supplier');
    Spn1 := FindParam(Comp, 'Supplier Part Number 1,Supplier Part Number');
    Comment := FindParam(Comp, 'Comment,Value,Description');

    MpnKey := UpperCaseStr(Mpn1);

    idx := MpnKeys.IndexOf(MpnKey);
    if idx < 0 then
    begin
      // First time we see this MPN -- add a new row.
      MpnKeys.Add(MpnKey);
      MpnOrig.Add(Mpn1);
      AccDesig.Add(Desig);
      AccComment.Add(Comment);
      AccMfr1.Add(Mfr1);
      AccSup1.Add(Sup1);
      AccSpn1.Add(Spn1);
      AccQty.Add('1');
    end
    else
    begin
      // Merge into existing row.

      // Quantity: increment by 1 per component occurrence.
      oldQty := StrToIntDef(AccQty[idx], 0);
      newQty := oldQty + 1;
      AccQty[idx] := IntToStr(newQty);

      // Designator: append if not already present (dedup by exact string).
      existing := AccDesig[idx];
      if Pos(Desig, existing) = 0 then
        AccDesig[idx] := existing + ', ' + Desig;

      // First non-empty wins for Comment.
      if (AccComment[idx] = '') and (Comment <> '') then
        AccComment[idx] := Comment;

      // First non-empty DKPN wins.
      if (AccSpn1[idx] = '') and (Spn1 <> '') then
        AccSpn1[idx] := Spn1;

      // Mfr1 and Sup1: keep first seen (same reasoning as DKPN).
      if (AccMfr1[idx] = '') and (Mfr1 <> '') then
        AccMfr1[idx] := Mfr1;
      if (AccSup1[idx] = '') and (Sup1 <> '') then
        AccSup1[idx] := Sup1;
    end;
  end;

  // ------------------------------------------------------------------
  // 5. Write CSV.
  //    [verified-on-AD26, hello-sentinel.pas 2026-05-14]
  // ------------------------------------------------------------------
  Lines := TStringList.Create;

  // Header row -- matches auto-detection in digikey_push.py:
  //   Designator -> DEFAULT_REF_COLS
  //   Comment    -> ignored by Python (human-readable column)
  //   Manufacturer 1 -> (not in DEFAULT_MPN_COLS; extra context column)
  //   Manufacturer Part Number 1 -> DEFAULT_MPN_COLS[0]
  //   Supplier 1 -> DEFAULT_SUPPLIER_COLS[0]
  //   Supplier Part Number 1 -> DEFAULT_DKPN_COLS[4]
  //   Quantity -> DEFAULT_QTY_COLS[0]
  Lines.Add('Designator,Comment,Manufacturer 1,Manufacturer Part Number 1,Supplier 1,Supplier Part Number 1,Quantity');

  RowCount := 0;
  TotalQty := 0;
  for ri := 0 to MpnKeys.Count - 1 do
  begin
    Lines.Add(
      CSVQuote(AccDesig[ri]) + ',' +
      CSVQuote(AccComment[ri]) + ',' +
      CSVQuote(AccMfr1[ri]) + ',' +
      CSVQuote(MpnOrig[ri]) + ',' +
      CSVQuote(AccSup1[ri]) + ',' +
      CSVQuote(AccSpn1[ri]) + ',' +
      AccQty[ri]
    );
    Inc(RowCount);
    TotalQty := TotalQty + StrToIntDef(AccQty[ri], 0);
  end;

  try
    Lines.SaveToFile(CsvPath);
  except
    MpnKeys.Free;
    MpnOrig.Free;
    AccDesig.Free;
    AccComment.Free;
    AccMfr1.Free;
    AccSup1.Free;
    AccSpn1.Free;
    AccQty.Free;
    Lines.Free;
    ShowMessage('Failed to write CSV.' + #13#10 + CsvPath);
    Exit;
  end;

  MpnKeys.Free;
  MpnOrig.Free;
  AccDesig.Free;
  AccComment.Free;
  AccMfr1.Free;
  AccSup1.Free;
  AccSpn1.Free;
  AccQty.Free;
  Lines.Free;

  // ------------------------------------------------------------------
  // 6. Report to user.
  // ------------------------------------------------------------------
  Msg := 'Wrote BOM to:' + #13#10 +
         '  ' + CsvPath + #13#10 + #13#10 +
         'Rows: ' + IntToStr(RowCount) +
         '  Total units: ' + IntToStr(TotalQty) + #13#10 + #13#10 +
         'Push to your DigiKey account:' + #13#10 +
         '  altium-push-to-digikey "' + CsvPath + '"' +
           ' --auth --list-name "' + ProjectName + '"' + #13#10 + #13#10 +
         'Or for the anonymous link-shareable mode:' + #13#10 +
         '  altium-push-to-digikey "' + CsvPath + '"';

  ShowMessage(Msg);
end;
