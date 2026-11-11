#import "probe.h"
// List a library's main roles with their palette colorIndex; with {"role","color"} recolor one
// main role via the Role Editor's own undoable action ("Edit Roles"). Subroles share the main role color.
// args: {"lib": "Library", "role": "§ Intro", "color": 9}
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  id lib=libNamed(a[@"lib"]);
  NSMutableArray *rows=[NSMutableArray new]; id target=nil;
  for(id role in CALL0(lib,@"mainRoles")){
    long ci=((long(*)(id,SEL))objc_msgSend)(role,NSSelectorFromString(@"colorIndex"));
    NSString *nm=D(CALL0(role,@"displayName")?:CALL0(role,@"roleName")?:CALL0(role,@"name")); id uid=CALL0(role,@"uid")?:CALL0(role,@"UID")?:CALL0(role,@"uuid");
    [rows addObject:[NSString stringWithFormat:@"%@ uid=%@ color=%ld",nm,D(uid),ci]];
    if([nm isEqualToString:a[@"role"]]) target=uid; }
  r[@"before"]=rows;
  if(target && a[@"color"]){
    @try{ ((void(*)(id,SEL,id,id,id,id,id,id))objc_msgSend)(lib,NSSelectorFromString(@"actionAddNewMainRoles:addNewSubRoles:reassignRoles:renameExistingRoles:changeColorOfExistingRoles:removeExistingRoles:"),nil,nil,nil,nil,@{target:a[@"color"]},nil); r[@"applied"]=@YES; }
    @catch(NSException *e){ r[@"exc"]=e.reason; }
    for(id role in CALL0(lib,@"mainRoles")) if([D(CALL0(role,@"displayName")?:CALL0(role,@"roleName")) isEqualToString:a[@"role"]]) r[@"after"]=@(((long(*)(id,SEL))objc_msgSend)(role,NSSelectorFromString(@"colorIndex")));
    r[@"undo"]=D(CALL0(CALL0(CALL0(timelineModule(),@"document"),@"undoManager"),@"undoActionName"));
  }
  if(!target) r[@"FFRoleSels"]=findSels(objc_getClass("FFRole"),@"id",NO);
  return r;
}
