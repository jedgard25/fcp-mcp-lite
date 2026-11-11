#import "probe.h"
#import <QuartzCore/QuartzCore.h>
static void findML(CALayer *l, NSMutableArray *o){ if([NSStringFromClass([l class]) isEqualToString:@"FFPlayerMetalLayer"]) [o addObject:l]; for(CALayer *s in l.sublayers) findML(s,o); }
static void findMLV(NSView *v, NSMutableArray *o){ if(v.layer) findML(v.layer,o); for(NSView *s in v.subviews) findMLV(s,o); }
static void writeScaled(CGImageRef cg, NSString *path, int W){
  int H=(int)round((double)W*CGImageGetHeight(cg)/CGImageGetWidth(cg));
  CGContextRef c=CGBitmapContextCreate(NULL,W,H,8,0,CGColorSpaceCreateWithName(kCGColorSpaceSRGB),kCGImageAlphaPremultipliedLast);
  CGContextSetInterpolationQuality(c,kCGInterpolationHigh); CGContextDrawImage(c,CGRectMake(0,0,W,H),cg);
  CGImageRef s=CGBitmapContextCreateImage(c); NSBitmapImageRep *rep=[[NSBitmapImageRep alloc] initWithCGImage:s];
  [[rep representationUsingType:NSBitmapImageFileTypeJPEG properties:@{NSImageCompressionFactor:@0.85}] writeToFile:path atomically:YES]; CGImageRelease(s); CGContextRelease(c);
}
NSDictionary *FCBProbe(NSDictionary *a){
  NSMutableDictionary *r=[NSMutableDictionary new];
  NSWindow *w=nil; for(NSWindow *x in [NSApp windows]) if([x.title isEqualToString:@"Final Cut Pro"]) w=x;
  NSMutableArray *ls=[NSMutableArray new]; findMLV(w.contentView,ls); CALayer *best=nil; for(CALayer *l in ls) if(!best||l.bounds.size.width>best.bounds.size.width) best=l;
  id dv=CALL0(best,@"destVideoDisplay"); id cont=CALL0(appDelegate(),@"activeEditorContainer");
  NSMutableArray *out=[NSMutableArray new];
  for(NSNumber *t in a[@"times"]){
    CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent();
    CMTime target=CMT([t doubleValue]);
    ((void(*)(id,SEL,CMTime))objc_msgSend)(cont,NSSelectorFromString(@"setPlayheadSequenceTime:"),target);
    id fr=nil; BOOL stale=YES; unsigned long long q=0; int tries=0;
    while(tries++<60){ [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.03]];
      CMTime now=((CMTime(*)(id,SEL))objc_msgSend)(cont,NSSelectorFromString(@"playheadSequenceTime"));
      fr=((id(*)(id,SEL,CMTime,double,double,CMTime,BOOL*,unsigned long long*))objc_msgSend)(dv,NSSelectorFromString(@"_copyPlayerFrameForTime:rate:tbRate:effFrameDur:retIsStale:retQSize:"),now,0.0,0.0,CMTimeMake(1001,30000),&stale,&q);
      if(fr && !stale && [D(fr) rangeOfString:@"(viewed)"].location!=NSNotFound){ CMTime ft=CMTimeMake(0,1); /* check time via description */ if([D(fr) rangeOfString:[NSString stringWithFormat:@"%.3f",CMTimeGetSeconds(now)]].location!=NSNotFound || tries>8) break; } }
    CGImageRef cg=(CGImageRef)((void*(*)(id,SEL))objc_msgSend)(CALL0(fr,@"image1"),NSSelectorFromString(@"newCGImageRef"));
    if(!cg){ [out addObject:[NSString stringWithFormat:@"t=%@ no image (%@)",t,D(fr)]]; continue; }
    writeScaled(cg,[a[@"dir"] stringByAppendingFormat:@"/comp_%@.jpg",t],[a[@"width"] intValue]?:960);
    [out addObject:[NSString stringWithFormat:@"t=%@ src %zux%zu tries=%d %.0fms | %@",t,CGImageGetWidth(cg),CGImageGetHeight(cg),tries,(CFAbsoluteTimeGetCurrent()-t0)*1000,[[D(fr) componentsSeparatedByString:@" (viewed)"] firstObject]]];
    CGImageRelease(cg);
  }
  r[@"frames"]=out; return r;
}
