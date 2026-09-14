// V.O.I.D Singularity — black-hole formation fragment shader (reference copy).
// Runtime-identical to window.VOID_SHADERS.fragment in ../shaders.js
// Everything is computed on the GPU: environment sampling, localized
// gravitational lensing, matter-attraction streaking, gravitational shadow,
// asymmetric accretion disk, photon ring, and warm plasma.
precision highp float;
varying vec2 vUv;
uniform vec2  u_res;
uniform float u_time, u_aspect;
uniform vec2  u_center;
uniform float u_formation, u_activity, u_bright;
uniform float u_distortStrength, u_distortRadius, u_envInteraction;
uniform float u_rotation, u_turbulence, u_plasma, u_bhScale;
uniform float u_interactionOn, u_useTex, u_texAspect;
uniform float u_transparent;   // 0 = opaque demo (env shown); 1 = transparent overlay (effect only)
uniform sampler2D u_tex;

const float PI=3.14159265;

// ---------- noise ----------
float hash21(vec2 p){ p=fract(p*vec2(123.34,345.45)); p+=dot(p,p+34.345); return fract(p.x*p.y); }
float vnoise(vec2 p){
  vec2 i=floor(p), f=fract(p);
  float a=hash21(i), b=hash21(i+vec2(1,0)), c=hash21(i+vec2(0,1)), d=hash21(i+vec2(1,1));
  vec2 u=f*f*(3.0-2.0*f);
  return mix(mix(a,b,u.x),mix(c,d,u.x),u.y);
}
float fbm(vec2 p){
  float s=0.0, a=0.5; mat2 m=mat2(1.6,1.2,-1.2,1.6);
  for(int i=0;i<6;i++){ s+=a*vnoise(p); p=m*p; a*=0.5; }
  return s;
}

// ---------- 1. ENVIRONMENT (procedural cosmos fallback OR wallpaper texture) ----------
vec3 environment(vec2 uv){
  if(u_useTex>0.5){
    // fit texture (cover) into screen space
    vec2 t=uv;
    float sa=u_aspect, ta=u_texAspect;
    if(sa>ta){ t.y=(t.y-0.5)*(ta/sa)+0.5; } else { t.x=(t.x-0.5)*(sa/ta)+0.5; }
    return texture2D(u_tex,t).rgb;
  }
  vec2 p=vec2(uv.x*u_aspect,uv.y);
  vec3 col=vec3(0.012,0.02,0.045);              // deep space base
  // broad nebula field (cool blues / teal / faint indigo)
  float n1=fbm(p*2.4+vec2(3.0,7.0));
  float n2=fbm(p*4.7-vec2(1.5,4.0)+n1);
  float neb=pow(smoothstep(0.35,0.95,n1*0.7+n2*0.5),1.4);
  vec3 c1=vec3(0.05,0.12,0.28);                 // blue
  vec3 c2=vec3(0.09,0.28,0.34);                 // teal
  vec3 c3=vec3(0.16,0.10,0.30);                 // faint indigo
  vec3 nebCol=mix(c1,c2,smoothstep(0.2,0.8,n2));
  nebCol=mix(nebCol,c3,smoothstep(0.55,1.0,n1)*0.6);
  col+=nebCol*neb*1.35;
  // bright cloud cores
  float core=pow(smoothstep(0.62,1.0,n2),3.0);
  col+=vec3(0.55,0.68,0.85)*core*0.5;
  // dust lanes (dark subtractive)
  float dust=fbm(p*3.3+vec2(9.0,2.0));
  col*=mix(1.0,0.45,smoothstep(0.5,0.85,dust)*0.8);
  // star layers
  for(int L=0;L<3;L++){
    float sc=90.0+float(L)*150.0;
    vec2 g=p*sc; vec2 id=floor(g); vec2 f=fract(g);
    float h=hash21(id+float(L)*17.3);
    float pr=smoothstep(0.982-float(L)*0.004,1.0,h);
    if(pr>0.0){
      vec2 sp=vec2(hash21(id+1.7),hash21(id+3.1));
      float d=length(f-sp);
      float tw=0.6+0.4*sin(u_time*(1.5+h*3.0)+h*30.0);
      float star=smoothstep(0.09,0.0,d)*pr*tw;
      vec3 sc2=mix(vec3(0.8,0.86,1.0),vec3(1.0,0.95,0.85),hash21(id+5.5));
      col+=sc2*star*(1.2-float(L)*0.25);
    }
  }
  return col;
}

// domain-warped turbulent plasma field, warm palette
vec3 plasmaColor(float t){
  // t in 0..1 -> deep crimson -> orange -> amber -> pale gold -> white-hot
  vec3 a=vec3(0.16,0.01,0.005);
  vec3 b=vec3(0.55,0.09,0.02);
  vec3 c=vec3(0.95,0.35,0.06);
  vec3 d=vec3(1.0,0.66,0.22);
  vec3 e=vec3(1.0,0.9,0.7);
  vec3 f=vec3(1.0,1.0,0.96);
  vec3 col=mix(a,b,smoothstep(0.0,0.25,t));
  col=mix(col,c,smoothstep(0.2,0.5,t));
  col=mix(col,d,smoothstep(0.45,0.72,t));
  col=mix(col,e,smoothstep(0.68,0.9,t));
  col=mix(col,f,smoothstep(0.88,1.0,t));
  return col;
}

mat2 rot(float a){ float s=sin(a),c=cos(a); return mat2(c,-s,s,c); }

void main(){
  vec2 uv=vUv;
  vec2 asp=vec2(u_aspect,1.0);
  vec2 P0=(uv-0.5)*asp;
  vec2 C =(u_center-0.5)*asp;
  vec2 d = P0-C;
  float r=length(d);
  vec2 dir = d/max(r,1e-4);
  float ang=atan(d.y,d.x);

  float f=u_formation;

  // phase weights derived from a single formation parameter
  float wDisturb = smoothstep(0.02,0.30,f);
  float wAttract = smoothstep(0.18,0.55,f);
  float wCompress= smoothstep(0.42,0.75,f);
  float wSing    = smoothstep(0.62,0.90,f);
  float wDisk    = smoothstep(0.72,1.0,f);

  // black-hole geometry (localized, scaled small vs. screen)
  float base = 0.150*u_bhScale;
  float rs   = base * smoothstep(0.50,0.92,f);              // shadow radius grows late
  float Rout = u_distortRadius;                              // gravity extent

  // ---------- 2. GRAVITATIONAL LENSING (localized displacement) ----------
  float falloff = smoothstep(Rout,0.0,r);                    // 1 at center -> 0 at edge
  falloff *= falloff;
  // lensing peaks during compression, then settles once the hole stabilizes
  float lensAmt = u_distortStrength*u_envInteraction*u_interactionOn
                * (0.30*wDisturb + 0.65*wCompress + 0.45*wSing - 0.32*wDisk);
  // deflection: pull the sampled source outward -> Einstein-ring magnification.
  // Bounded so the core never over-magnifies into a bright pinch.
  float bend = lensAmt * falloff * (0.040/(r+0.17));
  bend = min(bend, 0.30);
  // frame-dragging swirl, stronger near center, spins over time
  float swirl = (u_rotation)*falloff*(0.20+0.70*wCompress+0.40*wSing)*(0.055/(r+0.18))
              + u_time*u_rotation*0.045*falloff*(0.3+wSing);
  swirl *= u_interactionOn;

  float rSrc = r + bend;                       // magnified radius
  vec2 dRot = rot(swirl)*dir;                  // swirled direction
  vec2 srcP = C + dRot*rSrc;
  vec2 srcUV = srcP/asp + 0.5;

  // ---------- 3. matter attraction: streaking trails toward center ----------
  // Transparent desktop overlay: no environment to lens/streak (the real
  // wallpaper is behind the canvas and never sampled), so the whole
  // environment pipeline is skipped there — avoids painting a starfield over
  // the desktop and saves the procedural-cosmos + 4-tap streak GPU cost.
  vec3 env = vec3(0.0);
  if(u_transparent < 0.5){
    env = environment(srcUV);
    // streaking is a "matter falling in" transient — strong while forming, fades once stable
    float streak = (wAttract*0.7+wCompress*0.55)*(1.0-0.82*wDisk)*u_envInteraction*u_interactionOn*falloff;
    if(streak>0.01){
      float acc=1.0; float wsum=1.0;
      for(int i=1;i<=4;i++){
        float k=float(i)*0.020*streak;
        vec2 sp=(C + dRot*(rSrc+k))/asp+0.5;
        float w=1.0-float(i)*0.2;
        env += environment(sp)*w*streak*0.6;
        wsum+=w*streak*0.6;
      }
      env/=wsum;
      env*=(1.0+streak*0.25);
    }
    // slight inward brightening of compressed matter
    env += environment(srcUV)*wCompress*falloff*0.15;
    // matter approaching the horizon redshifts, then goes dark -> the shadow reads
    if(rs>0.0001){
      float cap=smoothstep(rs*0.95, rs*3.2, r);       // 0 at core -> 1 outside well
      env = mix(env*vec3(1.0,0.42,0.20), env, cap);   // redshift ring near horizon
      env *= mix(0.04, 1.0, cap);                      // darken toward the core
    }
  }

  vec3 col=env;
  vec3 emissive=vec3(0.0);   // warm plasma only (no environment) — drives the overlay alpha
  float shadowMask=0.0;      // horizon occlusion — darkens the desktop behind

  // ---------- 4. BLACK-HOLE RENDER ----------
  if(rs>0.0001){
    // accretion disk (asymmetric, turbulent). Face-on band around the shadow.
    float diskCenter=rs*2.15;
    float diskWidth =rs*1.55;
    float band=exp(-pow((r-diskCenter)/diskWidth,2.0));
    band += 0.5*exp(-pow((r-rs*1.35)/(rs*0.55),2.0));       // brighter inner rim

    // polar turbulence: domain-warped fbm, rotates over time
    float rotT=u_time*(0.35+0.4*u_activity)*u_rotation;
    vec2 pol=vec2(ang*1.6 + rotT + r*7.0, r*9.0 - rotT*0.6);
    float warp=fbm(pol*1.4);
    float turb=fbm(pol + warp*1.6*u_turbulence);
    float fil =fbm(pol*2.7 - warp*2.0*u_turbulence);         // fine filaments
    float plasma = (0.55+0.9*turb)*(0.65+0.7*fil);

    // Doppler-like asymmetry: one side hotter/brighter
    float beam = 0.5 + 0.95*pow(max(0.0,cos(ang-2.2)),1.4);

    float heat = clamp(plasma*beam*(1.25-smoothstep(rs*1.0,rs*3.4,r)), 0.0, 1.9);
    // hotter (whiter) toward inner edge & on the beamed side
    float temp = clamp(heat*0.5 + (1.0-smoothstep(rs*1.1,rs*2.6,r))*0.55 + beam*0.15, 0.0, 1.0);
    vec3 disk = plasmaColor(temp) * heat * band;

    // localized hot spots orbiting the disk
    float spotAng = u_time*(0.7+u_activity)*u_rotation;
    float spot = exp(-pow((r-diskCenter*0.92)/(rs*0.55),2.0))
               * pow(max(0.0,sin(ang*3.0 - spotAng)),8.0);
    disk += plasmaColor(0.97)*spot*1.1*(0.6+u_activity);

    disk *= u_plasma*(0.6+1.5*wDisk+0.6*wSing)*u_bright*2.3;

    // photon ring — thin bright warm ring just outside the shadow
    float ring=exp(-pow((r-rs*1.09)/(rs*0.11),2.0));
    vec3 photon=plasmaColor(0.96)*ring*(1.4+0.7*u_activity)*u_bright*(0.5+0.9*wSing)*1.9;

    // gravitational shadow — occlude background & plasma inside horizon
    float shadow=smoothstep(rs*1.05,rs*0.92,r);
    shadowMask=shadow;
    col=mix(col,vec3(0.0),shadow);

    // faint inner-edge glow hugging the shadow (photon build-up during singularity)
    float edge=exp(-pow((r-rs*1.2)/(rs*0.4),2.0))*(1.0-shadow);
    vec3 edgeC=plasmaColor(0.85)*edge*0.35*wSing*u_bright;
    col += edgeC; emissive += edgeC;

    vec3 diskC=disk*(1.0-shadow);
    vec3 photonC=photon*(1.0-shadow*0.6);
    col += diskC; emissive += diskC;
    col += photonC; emissive += photonC;
  }

  // subtle overall warm rim glow of the whole system (cheap ambient bloom seed)
  float sysGlow=exp(-pow(r/(rs*4.0+0.001),2.0));
  vec3 sysC=plasmaColor(0.6)*sysGlow*0.06*wDisk*u_bright;
  col += sysC; emissive += sysC;

  if(u_transparent > 0.5){
    // Effect-only composite over the transparent desktop: RGB is warm plasma
    // (black inside the horizon); alpha carries the horizon occlusion + plasma
    // luminance so the real wallpaper shows through everywhere the hole is not.
    // Premultiplied output (THREE uses premultipliedAlpha:true); fades to fully
    // transparent before formation so HIDDEN leaves zero residue.
    vec3 outc=mix(emissive,vec3(0.0),shadowMask);
    float eml=clamp(max(max(emissive.r,emissive.g),emissive.b),0.0,1.0);
    float a=clamp(max(shadowMask,eml),0.0,1.0)*smoothstep(0.01,0.12,f);
    gl_FragColor=vec4(outc*a, a);
  } else {
    gl_FragColor=vec4(col,1.0);
  }
}
