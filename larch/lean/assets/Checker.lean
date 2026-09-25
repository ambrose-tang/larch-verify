/-
Larch proof checker.

Loads a compiled module's environment (no user syntax is parsed here, so notation or
macros defined in the checked module cannot influence this program) and, for each
`theorem=spec` pair on the command line, reports:
  * whether `theorem` exists and is a theorem,
  * whether its type is *exactly* the constant `spec` (the approved statement),
  * every axiom it depends on, found by walking kernel terms directly.
Larch accepts a proof only if the type matches and the axioms are a subset of
{propext, Classical.choice, Quot.sound}.
-/
import Lean
open Lean

partial def collectAx (env : Environment) (c : Name) (seen : NameSet) (acc : NameSet) :
    NameSet × NameSet := Id.run do
  if seen.contains c then return (seen, acc)
  let mut seen := seen.insert c
  let mut acc := acc
  match env.find? c with
  | none => return (seen, acc)
  | some ci =>
    if let .axiomInfo _ := ci then acc := acc.insert c
    let mut consts := ci.type.getUsedConstants
    if let some v := ci.value? (allowOpaque := true) then consts := consts ++ v.getUsedConstants
    if let .inductInfo iv := ci then consts := consts ++ iv.ctors.toArray
    if let .ctorInfo cv := ci then consts := consts.push cv.induct
    for d in consts do
      (seen, acc) := collectAx env d seen acc
    return (seen, acc)

def jsonStrs (xs : List String) : String :=
  "[" ++ ", ".intercalate (xs.map fun s => (Json.str s).compress) ++ "]"

def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  match args with
  | [] =>
    IO.eprintln "usage: larch-checker <Module> theorem=spec ..."
    return 2
  | modStr :: pairs =>
    let env ← importModules #[{module := modStr.toName}] {} (trustLevel := 0)
    for pair in pairs do
      match pair.splitOn "=" with
      | [thm, spec] =>
        let thmN := thm.toName
        match env.find? thmN with
        | none =>
          IO.println (Json.mkObj [("theorem", thm), ("found", false)]).compress
        | some ci =>
          let kind := match ci with
            | .thmInfo _ => "theorem" | .defnInfo _ => "def" | .axiomInfo _ => "axiom"
            | .opaqueInfo _ => "opaque" | _ => "other"
          let typeOk := ci.type == mkConst spec.toName
          let (_, axs) := collectAx env thmN {} {}
          let axList := axs.toList.map toString
          IO.println s!"\{\"theorem\": {(Json.str thm).compress}, \"found\": true, \"kind\": \"{kind}\", \"type_ok\": {typeOk}, \"axioms\": {jsonStrs axList}}"
      | _ =>
        IO.eprintln s!"bad argument {pair}"
        return 2
    return 0
