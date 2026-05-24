{ push-bom-to-digikey-probe.pas
  10-line probe to confirm the Altium API surface used by
  push-bom-to-digikey.pas compiles and runs on the target AD26 install.

  HOW TO RUN
  ----------
  1. Open any .PrjPcb in Altium (project must be focused).
  2. Add this file to push-bom-to-digikey.PrjScr (or open it as a new
     script project).
  3. Run ProbeApiSurface from the Scripts panel.
  4. Read the ShowMessage output. A line starting with "FAIL:" means the
     symbol does not exist on this AD26 install -- do not ship the main
     script until investigated.

  Symbols under test (all [pattern] -- not yet bench-verified on AD26):
    Project.DM_DocumentFlattened  -> IDocument
    FlatDoc.DM_ComponentCount     -> Integer
    FlatDoc.DM_Components[0]      -> IComponent
    IComponent.DM_ParameterCount  -> Integer
    IComponent.DM_Parameters(0)   -> IParameter
    IParameter.DM_Name            -> String
    IParameter.DM_Value           -> String
    IComponent.SourceDesignator   -> String
    Ord(IComponent.GetState_ComponentKind) -> Integer (expect 0..5)
    IProject.DM_CurrentProjectVariant -> IProjectVariant (nil = no variant)
    IProject.DM_ProjectFileName   -> String (bare filename)
}

procedure ProbeApiSurface;
var
  Workspace : IWorkspace;
  Project   : IProject;
  Variant   : IProjectVariant;
  FlatDoc   : IDocument;
  Comp      : IComponent;
  Parm      : IParameter;
  Report    : TStringList;
  ok        : Boolean;
  msg       : String;
  ri        : Integer;
begin
  Report := TStringList.Create;
  Report.Add('push-bom-to-digikey API probe -- ' + DateTimeToStr(Now));
  Report.Add('');

  ok := True;

  Workspace := GetWorkspace;
  if Workspace = nil then
  begin
    Report.Add('FAIL: GetWorkspace returned nil');
    ok := False;
  end
  else
    Report.Add('PASS: GetWorkspace');

  if ok then
  begin
    Project := Workspace.DM_FocusedProject;
    if Project = nil then
    begin
      Report.Add('FAIL: DM_FocusedProject returned nil (open a .PrjPcb first)');
      ok := False;
    end
    else
      Report.Add('PASS: DM_FocusedProject -> ' + Project.DM_ProjectFullPath);
  end;

  if ok then
  begin
    Report.Add('INFO: DM_ProjectFileName = ' + Project.DM_ProjectFileName);
    Variant := Project.DM_CurrentProjectVariant;
    if Variant = nil then
      Report.Add('INFO: DM_CurrentProjectVariant = nil (no variant / base design)')
    else
      Report.Add('PASS: DM_CurrentProjectVariant -> ' + Variant.DM_Description);
  end;

  if ok then
  begin
    FlatDoc := Project.DM_DocumentFlattened;
    if FlatDoc = nil then
    begin
      Report.Add('INFO: DM_DocumentFlattened nil -- trying compile...');
      ResetParameters;
      AddStringParameter('ObjectKind', 'Project');
      RunProcess('WorkspaceManager:Compile');
      FlatDoc := Project.DM_DocumentFlattened;
    end;
    if FlatDoc = nil then
    begin
      Report.Add('FAIL: DM_DocumentFlattened still nil after compile');
      ok := False;
    end
    else
      Report.Add('PASS: DM_DocumentFlattened -> ComponentCount=' +
                 IntToStr(FlatDoc.DM_ComponentCount));
  end;

  if ok and (FlatDoc.DM_ComponentCount > 0) then
  begin
    Comp := FlatDoc.DM_Components[0];
    if Comp = nil then
    begin
      Report.Add('FAIL: DM_Components[0] returned nil');
      ok := False;
    end
    else
    begin
      Report.Add('PASS: DM_Components[0].SourceDesignator = ' +
                 Comp.SourceDesignator);
      Report.Add('INFO: GetState_ComponentKind ordinal = ' +
                 IntToStr(Ord(Comp.GetState_ComponentKind)));
      Report.Add('INFO: DM_ParameterCount = ' +
                 IntToStr(Comp.DM_ParameterCount));
      if Comp.DM_ParameterCount > 0 then
      begin
        Parm := Comp.DM_Parameters(0);
        if Parm = nil then
          Report.Add('FAIL: DM_Parameters(0) returned nil')
        else
          Report.Add('PASS: DM_Parameters(0).DM_Name=' + Parm.DM_Name +
                     '  DM_Value=' + Parm.DM_Value);
      end;
    end;
  end;

  Report.Add('');
  if ok then
    Report.Add('Overall: PASS -- API surface appears accessible.')
  else
    Report.Add('Overall: FAIL -- see FAIL lines above.');

  msg := '';
  // Concatenate report lines for ShowMessage.
  // TStringList.Text is not used here because it may insert CRLF in a
  // version-dependent way; explicit loop is safer.
  for ri := 0 to Report.Count - 1 do
    msg := msg + Report[ri] + #13#10;

  ShowMessage(msg);
  Report.Free;
end;
